# -*- coding: utf-8 -*-
"""A股情绪 & 同花顺概念题材面板（本地DB驱动）

- 热股：按“当日成交额TopN股票”的平均涨跌幅减去“全A等权”
- 全市场成交额：上证综指(000001) + 深证成指(399001) 指数成交额之和
- ETF：读取本地 etf_equity.db（按实际表头自适应），输出“份额日增幅(%)”
- 题材：同花顺概念指数（ths_concept.db），显示概念名称 + temperature + risk + 当日成交额占比（概念amount / 市场成交额）
- 计算结果保存：daily_metrics_last40.csv（最近40个交易日）

注意：
1) 本程序默认不在面板中做“全市场股票日K全量更新”（太重）。你应当用单独 backfill 脚本更新 a/ashare_mvp.db。
2) 本程序只基于本地DB计算，若部分因子缺历史会显示缺失提示，但不会把报错/对象repr显示在页面上。
"""

import os
import sqlite3
import datetime as dt
import re
import math
import subprocess
import requests
import sys
from pathlib import Path
from typing import Optional, Dict, List, Tuple, Any

import numpy as np
import pandas as pd
import streamlit as st

from runtime_paths import build_runtime_paths, ensure_runtime_dirs


def percentile_midrank(hist: pd.Series, x: float) -> Optional[float]:
    """
    Percentile using mid-rank plotting position:
      p = (count(<x) + 0.5*count(==x)) / n
    Returns p in [0,1]. This avoids exact 0/1 for extreme observations
    when n>0 and there is at least one equal observation.
    """
    try:
        s = pd.to_numeric(hist, errors="coerce").dropna()
        if s.empty or x is None or (isinstance(x, float) and pd.isna(x)):
            return None
        n = len(s)
        less = float((s < x).sum())
        equal = float((s == x).sum())
        if equal == 0:
            # if x not in sample, approximate using <=
            return float((s <= x).mean())
        p = (less + 0.5 * equal) / n
        return max(0.0, min(1.0, p))
    except Exception:
        return None


DEFAULT_STOCK_DB_CANDIDATES = ["ashare_mvp.db", "a_share_mvp.db", "a/ashare_mvp.db", "a/a_share_mvp.db", "./a/ashare_mvp.db", "./a/a_share_mvp.db"]
DEFAULT_CONCEPT_DB = "ths_concept.db"
DEFAULT_ETF_DB = "etf_mvp.db"
DEFAULT_CSV_OUT = "daily_metrics_last40.csv"


# Backfill scripts (place them in the same folder as this Streamlit app)
# We keep a small list of candidate filenames to avoid breaking when you version-bump scripts.
SCRIPT_BAOSTOCK_CANDIDATES = [
    "backfill_baostock_hsA_60d_v2.py",
    "backfill_baostock_hsA_60d.py",
]
SCRIPT_CONCEPT_CANDIDATES = [
    "backfill_adata_ths_concept_index_kline_60d_v3.py",
    "backfill_adata_ths_concept_index_kline_60d_v2.py",
    "backfill_adata_ths_concept_index_kline_60d.py",
]
SCRIPT_ETF_CANDIDATES = [
    "backfill_etf_equity_60d_v2.py",
    "backfill_etf_equity_60d.py",
]

# Only treat these as A-share stock codes (prevents ETF codes being mis-tagged as 'stock')
A_SHARE_PREFIX_RE = re.compile(r"^(600|601|603|605|688|000|001|002|003|300|301)\d{3}$")

REQUIRED_INDEX_CODES = ['000001','399001','000300','000852']


CODE_SH000001 = "000001"
CODE_SZ399001 = "399001"

CODE_HS300 = "000300"
CODE_ZZ1000 = "000852"


# ----------------------------
# Basic helpers
# ----------------------------

# ----------------------------
# Startup backfill & persistence helpers
# ----------------------------
def app_dir() -> str:
    try:
        return os.path.dirname(os.path.abspath(__file__))
    except Exception:
        return os.getcwd()


def abs_in_app_dir(filename: str) -> str:
    if os.path.isabs(filename):
        return filename
    return os.path.join(app_dir(), filename)

def pick_existing_script(candidates: List[str]) -> Optional[str]:
    """Return the first existing script path.

    Search order (lightweight, no recursion):
      1) app folder (same directory as this file)
      2) current working directory
      3) ./scripts under app folder
      4) ./scripts under current working directory

    This fixes a common failure mode where backfill scripts are stored next to the Streamlit
    launcher (cwd) rather than next to this app file.
    """
    roots = [
        app_dir(),
        os.getcwd(),
        os.path.join(app_dir(), "scripts"),
        os.path.join(os.getcwd(), "scripts"),
    ]
    for name in candidates:
        if os.path.isabs(name) and os.path.exists(name):
            return name
        for root in roots:
            sp = os.path.join(root, name)
            if os.path.exists(sp):
                return sp
    return None



@st.cache_data(ttl=3600, show_spinner=False)
def read_filtered_concepts_csv() -> dict:
    """Read filtered_concept.csv (codes to exclude) in app folder / current working directory.

    Supports column names like: index_code / concept_code / code / 指数代码 / 概念代码.
    Returns mapping {6-digit-code: name(optional)}.
    """
    # Search file in both app dir and cwd (people often run streamlit from another folder).
    candidates = [abs_in_app_dir("filtered_concept.csv"), os.path.join(os.getcwd(), "filtered_concept.csv")]
    fp = next((p for p in candidates if os.path.exists(p)), None)
    if not fp:
        return {}
    # robust CSV read (utf-8/gbk)
    df = None
    try:
        df = pd.read_csv(fp, dtype=str, encoding="utf-8")
    except Exception:
        try:
            df = pd.read_csv(fp, dtype=str, encoding="gbk")
        except Exception:
            return {}
    if df is None or df.empty:
        return {}

    cols = list(df.columns)

    def _pick(col_names: List[str]) -> Optional[str]:
        low = {str(c).strip().lower(): c for c in cols}
        for n in col_names:
            k = str(n).strip().lower()
            if k in low:
                return low[k]
        # fuzzy contains
        for c in cols:
            cl = str(c).lower()
            if "code" in cl or "指数" in cl or "概念" in cl:
                return c
        return None

    code_col = _pick(["index_code", "concept_code", "code", "指数代码", "概念代码", "index", "idx_code"])
    if code_col is None:
        return {}
    name_col = _pick(["name", "index_name", "concept_name", "名称", "指数名称", "概念名称"])

    def _norm_code(x) -> str:
        s = str(x).strip() if x is not None else ""
        if s == "" or s.lower() in ["nan", "none"]:
            return ""
        digits = re.sub(r"\D", "", s)
        if digits == "":
            return ""
        # common Excel float like '886107.0' -> '8861070' : drop the last trailing 0
        if s.endswith(".0") and len(digits) == 7 and digits.endswith("0"):
            digits = digits[:-1]
        # if still longer than 6, keep the first 6 digits (THS/SZ/SSE codes are 6d)
        if len(digits) > 6:
            digits = digits[:6]
        return digits.zfill(6)

    df = df.copy()
    df["__code__"] = df[code_col].map(_norm_code)
    df = df[df["__code__"].str.len() == 6].drop_duplicates(subset=["__code__"])
    if df.empty:
        return {}
    if name_col and name_col in df.columns:
        df["__name__"] = df[name_col].astype(str).str.strip()
        return dict(zip(df["__code__"], df["__name__"]))
    return {c: "" for c in df["__code__"].tolist()}


def filtered_concept_code_set() -> set:
    """Convenience wrapper returning set of filtered concept codes (6-digit strings)."""
    try:
        return set(read_filtered_concepts_csv().keys())
    except Exception:
        return set()


def run_cmd(cmd: List[str], timeout_sec: int = 3600) -> Tuple[int, str]:
    """Run command and return (returncode, combined_output)."""
    try:
        p = subprocess.run(
            cmd,
            cwd=app_dir(),
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
        out = (p.stdout or "") + ("\n" + p.stderr if p.stderr else "")
        return int(p.returncode), out.strip()
    except Exception as e:
        return 999, f"run failed: {e}"


def try_get_last_trade_day_baostock(include_today: bool, tz_offset_hours: int = 8) -> Optional[str]:
    """Return the last trading day using baostock calendar.

    - If include_today=True: return last trading day <= CN 'today' (by tz_offset_hours).
    - If include_today=False: return last trading day < CN 'today'.
    This aligns with dashboard needs:
      * 09:30-17:30: prefer last completed close (usually yesterday)
      * 17:30-09:30: prefer most recent close (today after close; otherwise yesterday)
    """
    try:
        import baostock as bs  # type: ignore
    except Exception:
        return None
    try:
        lg = bs.login()
        if getattr(lg, "error_code", "") != "0":
            return None

        cn_today = _now_with_tz_offset(int(tz_offset_hours)).strftime("%Y-%m-%d")
        start = (pd.Timestamp(cn_today) - pd.Timedelta(days=60)).strftime("%Y-%m-%d")

        rs = bs.query_trade_dates(start_date=start, end_date=cn_today)
        items: List[str] = []
        while rs.error_code == "0" and rs.next():
            row = rs.get_row_data()
            if len(row) >= 2 and str(row[1]) == "1":
                items.append(str(row[0]))

        if not items:
            return None

        if include_today:
            items = [d for d in items if d <= cn_today]
        else:
            items = [d for d in items if d < cn_today]

        return items[-1] if items else None
    except Exception:
        return None
    finally:
        try:
            bs.logout()
        except Exception:
            pass


def get_source_last_trade_day(tz_offset_hours: int = 8) -> str:
    """Single source-of-truth trade day for this dashboard.

    Rule:
      - After close (>=17:30 CN): allow 'today' as last close day if it's a trading day.
      - Otherwise: use the last trading day strictly before today.
    NOTE: This is a *calendar* day; actual daily-K availability is still validated by DB checks.
    """
    now_cn = _now_with_tz_offset(int(tz_offset_hours))
    include_today = bool(_is_after_close_cn(now_cn, int(tz_offset_hours)))
    d = try_get_last_trade_day_baostock(include_today=include_today, tz_offset_hours=int(tz_offset_hours))
    if d:
        return d

    # Fallback: weekday-only approximation (no holiday awareness)
    t = pd.Timestamp(now_cn.strftime("%Y-%m-%d"))
    while t.weekday() >= 5:
        t -= pd.Timedelta(days=1)
    if (not include_today) and (t.strftime("%Y-%m-%d") == now_cn.strftime("%Y-%m-%d")):
        t -= pd.Timedelta(days=1)
        while t.weekday() >= 5:
            t -= pd.Timedelta(days=1)
    return t.strftime("%Y-%m-%d")

def is_date_behind(local_d: Optional[str], remote_d: Optional[str]) -> bool:
    if not remote_d:
        return False
    if not local_d:
        return True
    try:
        return pd.Timestamp(str(local_d).replace("/", "-")[:10]) < pd.Timestamp(str(remote_d).replace("/", "-")[:10])
    except Exception:
        return True


def try_get_trade_dates_baostock(start_date: str, end_date: str) -> List[str]:
    try:
        import baostock as bs  # type: ignore
    except Exception:
        return []
    try:
        lg = bs.login()
        if getattr(lg, "error_code", "") != "0":
            return []
        rs = bs.query_trade_dates(start_date=str(start_date), end_date=str(end_date))
        items: List[str] = []
        while rs.error_code == "0" and rs.next():
            row = rs.get_row_data()
            if len(row) >= 2 and str(row[1]) == "1":
                items.append(str(row[0]).replace("/", "-")[:10])
        return items
    except Exception:
        return []
    finally:
        try:
            bs.logout()
        except Exception:
            pass


def get_prev_trade_days(anchor_day: str, n_back: int = 2) -> List[str]:
    if not anchor_day:
        return []
    try:
        a = pd.Timestamp(str(anchor_day).replace("/", "-")[:10]).date()
    except Exception:
        return []
    start = (a - dt.timedelta(days=90)).strftime("%Y-%m-%d")
    end = a.strftime("%Y-%m-%d")
    cal = try_get_trade_dates_baostock(start, end)
    if cal:
        cal = [d for d in cal if d <= end]
        if not cal:
            return []
        t = cal[-1]
        out = [t]
        for _ in range(n_back):
            if len(cal) >= len(out) + 1:
                out.append(cal[-(len(out) + 1)])
            else:
                break
        return out
    # weekday fallback
    out: List[str] = []
    t = pd.Timestamp(end).date()
    while t.weekday() >= 5:
        t -= dt.timedelta(days=1)
    out.append(t.strftime("%Y-%m-%d"))
    for _ in range(n_back):
        t -= dt.timedelta(days=1)
        while t.weekday() >= 5:
            t -= dt.timedelta(days=1)
        out.append(t.strftime("%Y-%m-%d"))
    return out


@st.cache_data(ttl=300, show_spinner=False)
def discover_data_dates_bundle(concept_db: Optional[str], tz_offset_hours: int = 8) -> Dict[str, Optional[str]]:
    cal_last = get_source_last_trade_day(tz_offset_hours=int(tz_offset_hours))

    remote_stock = get_remote_latest_close_day_stock_indexes() or cal_last
    remote_concept = get_remote_latest_close_day_concept(concept_db) or remote_stock or cal_last
    remote_etf = remote_stock or cal_last

    candidates = [d for d in [remote_stock, remote_concept, remote_etf] if d]
    t1 = min(candidates) if candidates else cal_last

    prevs = get_prev_trade_days(t1, n_back=2)
    t1_aligned = prevs[0] if prevs else t1
    t2 = prevs[1] if len(prevs) >= 2 else None

    return {
        "calendar_last_trade_day": cal_last,
        "remote_latest_stock_close_day": remote_stock,
        "remote_latest_concept_close_day": remote_concept,
        "remote_latest_etf_close_day": remote_etf,
        "t1_close_day": t1_aligned,
        "t2_close_day": t2,
    }


def sql_max_date(con: sqlite3.Connection, sql: str, params: Tuple = ()) -> Optional[str]:
    try:
        r = con.execute(sql, params).fetchone()
        if not r:
            return None
        v = r[0]
        if v is None:
            return None
        return str(v).replace("/", "-")[:10]
    except Exception:
        return None


def get_local_last_dates(stock_db: str, concept_db: Optional[str], etf_db: Optional[str]) -> Dict[str, Optional[str]]:
    out: Dict[str, Optional[str]] = {"stock": None, "index": None, "concept": None, "etf": None}
    try:
        con = connect(stock_db)
        if table_exists(con, "kline_daily"):
            out["stock"] = sql_max_date(con, "SELECT MAX(trade_date) FROM kline_daily WHERE sec_type='stock'")
            out["index"] = sql_max_date(
                con,
                "SELECT MAX(trade_date) FROM kline_daily WHERE sec_type='index' AND sec_code IN (?,?,?,?)",
                tuple(REQUIRED_INDEX_CODES),
            )
        con.close()
    except Exception:
        pass

    if concept_db and os.path.exists(concept_db):
        try:
            con = connect(concept_db)
            if table_exists(con, "concept_kline"):
                out["concept"] = sql_max_date(con, "SELECT MAX(substr(replace(trade_date, '/', '-'),1,10)) FROM concept_kline")
            elif table_exists(con, "concept_kline_ths"):
                out["concept"] = sql_max_date(con, "SELECT MAX(substr(replace(trade_date, '/', '-'),1,10)) FROM concept_kline_ths")
            con.close()
        except Exception:
            pass

    if etf_db and os.path.exists(etf_db):
        try:
            con = connect(etf_db)
            if table_exists(con, "etf_total"):
                out["etf"] = sql_max_date(con, "SELECT MAX(trade_date) FROM etf_total")
            con.close()
        except Exception:
            pass
    return out


def cleanup_stock_db_remove_non_a_share(stock_db: str) -> int:
    """Remove rows incorrectly tagged as stock (e.g. ETF codes 15xxxx/51xxxx/56xxxx etc)."""
    try:
        con = connect(stock_db)
        if not table_exists(con, "kline_daily"):
            con.close()
            return 0
        # non-numeric or wrong length
        cur = con.execute(
            "DELETE FROM kline_daily WHERE sec_type='stock' AND (sec_code IS NULL OR LENGTH(sec_code)<>6 OR sec_code GLOB '*[^0-9]*')"
        )
        n1 = cur.rowcount or 0
        # numeric but not matching A-share prefixes
        cur = con.execute(
            "DELETE FROM kline_daily WHERE sec_type='stock' AND sec_code NOT LIKE '000%' AND sec_code NOT LIKE '001%' "
            "AND sec_code NOT LIKE '002%' AND sec_code NOT LIKE '003%' AND sec_code NOT LIKE '300%' AND sec_code NOT LIKE '301%' "
            "AND sec_code NOT LIKE '600%' AND sec_code NOT LIKE '601%' AND sec_code NOT LIKE '603%' AND sec_code NOT LIKE '605%' AND sec_code NOT LIKE '688%'"
        )
        n2 = cur.rowcount or 0
        con.commit()
        con.close()
        return int(n1 + n2)
    except Exception:
        return 0


def ensure_daily_matrics_schema(con: sqlite3.Connection) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS daily_matrics (
          trade_date TEXT PRIMARY KEY
        )
        """
    )
    cols = set(get_columns(con, "daily_matrics"))
    need_cols = {
        "sentiment_score": "REAL",
        "momentum": "REAL",
        "momentum_z": "REAL",
        "adv_dec_ratio": "REAL",
        "ew_ret": "REAL",
        "adv_ratio": "REAL",
        "hot_excess": "REAL",
        "small_big_rel": "REAL",
        "etf_share_pct": "REAL",
        "turnover_rel_ma10": "REAL",
        "updated_at": "TEXT",
    }
    for c, typ in need_cols.items():
        if c not in cols:
            con.execute(f"ALTER TABLE daily_matrics ADD COLUMN {c} {typ}")
    con.commit()


def upsert_daily_matrics(con: sqlite3.Connection, df: pd.DataFrame) -> int:
    if df is None or df.empty:
        return 0
    ensure_daily_matrics_schema(con)
    cols = set(get_columns(con, "daily_matrics"))
    d = df.copy()
    d["trade_date"] = pd.to_datetime(d["trade_date"]).dt.strftime("%Y-%m-%d")
    d["updated_at"] = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    candidates = [
        "trade_date","sentiment_score","momentum","momentum_z","adv_dec_ratio","ew_ret","adv_ratio",
        "hot_excess","small_big_rel","etf_share_pct","turnover_rel_ma10","updated_at"
    ]
    write_cols = [c for c in candidates if c in cols and c in d.columns]
    if "trade_date" not in write_cols:
        write_cols = ["trade_date"] + write_cols
    ph = ",".join(["?"] * len(write_cols))
    sql = f"INSERT OR REPLACE INTO daily_matrics ({','.join(write_cols)}) VALUES ({ph})"
    rows = list(d[write_cols].itertuples(index=False, name=None))
    con.executemany(sql, rows)
    con.commit()
    return len(rows)




# ----------------------------
# Remote (interface) latest close-day discovery (daily close endpoints)
# ----------------------------

def _date_str(d: dt.date) -> str:
    return d.strftime("%Y-%m-%d")


def try_get_latest_kline_day_baostock(code: str, lookback_days: int = 25) -> Optional[str]:
    """Query baostock daily endpoint for `code` and return the latest `date` observed.
    This reflects *data availability* on the interface, not just the trade calendar.
    """
    try:
        import baostock as bs  # type: ignore
        end = dt.date.today()
        start = end - dt.timedelta(days=lookback_days)
        lg = bs.login()
        if getattr(lg, "error_code", "0") != "0":
            try:
                bs.logout()
            except Exception:
                pass
            return None
        rs = bs.query_history_k_data_plus(
            code,
            fields="date",
            start_date=_date_str(start),
            end_date=_date_str(end),
            frequency="d",
            adjustflag="3",
        )
        latest = None
        while rs.error_code == "0" and rs.next():
            d = rs.get_row_data()[0]
            if d:
                latest = d if (latest is None or d > latest) else latest
        try:
            bs.logout()
        except Exception:
            pass
        return latest
    except Exception:
        return None


def get_remote_latest_close_day_stock_indexes() -> Optional[str]:
    """Return the latest close day that is available for *both* 000001 & 399001 daily endpoints.
    Uses the minimum of the two latest dates as the common available close day.
    """
    d1 = try_get_latest_kline_day_baostock("sh.000001")
    d2 = try_get_latest_kline_day_baostock("sz.399001")
    ds = [d for d in [d1, d2] if d]
    if not ds:
        return None
    return min(ds)


def _sample_concept_codes_from_db(concept_db: str, limit: int = 10) -> List[str]:
    codes: List[str] = []
    try:
        con = connect(concept_db)
        table = None
        if table_exists(con, "concept_kline_ths"):
            table = "concept_kline_ths"
        elif table_exists(con, "concept_kline"):
            table = "concept_kline"
        if not table:
            con.close()
            return codes
        schema = detect_concept_kline_schema(con, table)
        code_col = schema.get("code")
        if not code_col:
            con.close()
            return codes
        rows = con.execute(f"SELECT DISTINCT {code_col} FROM {table} WHERE {code_col} IS NOT NULL LIMIT ?", (limit,)).fetchall()
        con.close()
        for r in rows or []:
            if r and r[0]:
                codes.append(canonical_code(str(r[0])))
    except Exception:
        return codes
    # de-dup, keep order
    seen = set()
    out = []
    for c in codes:
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def try_get_latest_day_adata_market(stock_code: str, lookback_days: int = 45) -> Optional[str]:
    """Query adata daily endpoint (stock.market.get_market) and return latest trade_date (YYYY-MM-DD).

    IMPORTANT:
      - Some DBs store dates like 'YYYY/MM/DD'. Some APIs may return 'YYYYMMDD' or 'YYYY-MM-DD'.
      - We must take the *chronological* max, not the lexicographic max.
    """
    try:
        import adata  # type: ignore
        start = _date_str(dt.date.today() - dt.timedelta(days=lookback_days))
        df = adata.stock.market.get_market(stock_code=stock_code, k_type=1, start_date=start)  # type: ignore
        if df is None or getattr(df, "empty", True):
            return None
        col = "trade_date" if "trade_date" in df.columns else None
        if not col:
            return None
        s = df[col].dropna().astype(str)
        if s.empty:
            return None
        # normalize separators and slice to 10 chars when possible
        s2 = s.str.replace("/", "-", regex=False).str.strip()
        # handle YYYYMMDD
        s2 = s2.apply(lambda x: f"{x[0:4]}-{x[4:6]}-{x[6:8]}" if re.fullmatch(r"\d{8}", x) else x[:10])
        ts = pd.to_datetime(s2, errors="coerce")
        if ts.isna().all():
            return None
        d = ts.max().date()
        return d.strftime("%Y-%m-%d")
    except Exception:
        return None
    except Exception:
        return None


def get_remote_latest_close_day_concept(concept_db: Optional[str]) -> Optional[str]:
    """Discover the latest close day available on the adata daily endpoint for THS concept indices.
    We sample a few concept codes from local DB and probe adata. If probing fails, returns None.
    """
    if not concept_db or (not os.path.exists(concept_db)):
        return None
    codes = _sample_concept_codes_from_db(concept_db, limit=12)
    for c in codes:
        d = try_get_latest_day_adata_market(c)
        if d:
            return d
    return None


def calc_backfill_days(local_d: Optional[str], remote_d: Optional[str], cap: int = 15) -> int:
    """Small incremental backfill: missing_days + buffer, capped for performance."""
    if not remote_d:
        return 0
    if not local_d:
        return 60
    try:
        ld = pd.Timestamp(local_d).date()
        rd = pd.Timestamp(remote_d).date()
        delta = (rd - ld).days
        if delta <= 0:
            return 0
        # buffer 2 days, min 5 days
        return int(min(max(delta + 2, 5), cap))
    except Exception:
        return min(60, cap)



def maybe_backfill_all(stock_db: str, concept_db: Optional[str], etf_db: Optional[str], force: bool = False) -> Tuple[Dict[str, Optional[str]], List[str]]:
    """Update DBs via *daily close* endpoints (baostock/adata) if local DB is behind the interface.
    - Stock/index DB: baostock daily kline (with indexes)
    - Concept DB: adata daily kline (THS concept index)
    - ETF DB: daily update script (if present)

    Key change (v42):
      - Determine remote latest close day PER DOMAIN by probing the interface, not only by trade calendar.
      - If stock DB is up-to-date but concept DB is behind, only backfill concept (and vice versa).
      - Backfill window is incremental (missing_days + small buffer, capped) to keep it lightweight.
    """
    logs: List[str] = []

    # Trade calendar fallback (cheap). Interface probing may return None.
    cal_last = get_source_last_trade_day()
    remote_stock = get_remote_latest_close_day_stock_indexes() or cal_last
    remote_concept = get_remote_latest_close_day_concept(concept_db) or remote_stock or cal_last
    remote_etf = remote_stock or cal_last

    logs.append(f"calendar_last_trade_day={cal_last}")
    logs.append(f"remote_latest_stock_close_day={remote_stock}")
    logs.append(f"remote_latest_concept_close_day={remote_concept}")
    logs.append(f"remote_latest_etf_close_day={remote_etf}")

    local = get_local_last_dates(stock_db, concept_db, etf_db)
    logs.append(f"local_last_dates={local}")

    def _is_behind(local_d: Optional[str], remote_d: Optional[str]) -> bool:
        if not remote_d:
            return False
        if not local_d:
            return True
        try:
            return pd.Timestamp(local_d) < pd.Timestamp(remote_d)
        except Exception:
            return True

    # Gap-based incremental windows
    # For stock we consider both stock and index; take the earlier of the two as the reference for gap.
    local_stock_ref = local.get("stock") or local.get("index")
    if local.get("stock") and local.get("index"):
        try:
            local_stock_ref = min(local.get("stock"), local.get("index"))
        except Exception:
            local_stock_ref = local.get("stock") or local.get("index")

    days_stock = 60 if force else calc_backfill_days(local_stock_ref, remote_stock, cap=15)
    days_concept = 60 if force else calc_backfill_days(local.get("concept"), remote_concept, cap=15)
    days_etf = 60 if force else calc_backfill_days(local.get("etf"), remote_etf, cap=15)

    need_stock = force or _is_behind(local.get("stock"), remote_stock) or _is_behind(local.get("index"), remote_stock)
    need_concept = force or _is_behind(local.get("concept"), remote_concept)
    need_etf = force or _is_behind(local.get("etf"), remote_etf)

    # Ensure required indices exist on remote_stock day (quality check)
    try:
        con = connect(stock_db)
        ok = True
        for c in REQUIRED_INDEX_CODES:
            r = con.execute(
                "SELECT 1 FROM kline_daily WHERE sec_type='index' AND sec_code=? AND trade_date=? LIMIT 1",
                (c, remote_stock),
            ).fetchone()
            if r is None:
                ok = False
                break
        con.close()
        if not ok:
            need_stock = True
            logs.append("index quality check failed for remote_stock day -> will backfill indexes")
    except Exception:
        pass

    # -------- stock/index backfill (baostock daily close) --------
    if need_stock and remote_stock:
        sp = pick_existing_script(SCRIPT_BAOSTOCK_CANDIDATES)
        if sp:
            d = str(max(days_stock, 5)) if (not force) else "60"
            cmd = [sys.executable, sp, "--db", stock_db, "--days", d, "--asof", remote_stock, "--with-indexes"]
            rc, out = run_cmd(cmd)
            logs.append(f"[BAOSTOCK] rc={rc} days={d} asof={remote_stock}")
            if out:
                logs.append(out[:2000])

            # post-check: required indices on remote_stock
            try:
                con2 = connect(stock_db)
                missing = []
                for c in REQUIRED_INDEX_CODES:
                    r = con2.execute(
                        "SELECT 1 FROM kline_daily WHERE sec_type='index' AND sec_code=? AND trade_date=? LIMIT 1",
                        (c, remote_stock),
                    ).fetchone()
                    if r is None:
                        missing.append(c)
                con2.close()
                if missing:
                    logs.append(f"[BAOSTOCK] warning: still missing required indices on {remote_stock}: {missing}")
            except Exception:
                pass
        else:
            logs.append("baostock backfill script not found -> skip stock/index daily DB update")

    # -------- concept backfill (adata daily close) --------
    if need_concept and remote_concept and concept_db:
        sp = pick_existing_script(SCRIPT_CONCEPT_CANDIDATES)
        if sp:
            d = str(max(days_concept, 5)) if (not force) else "60"
            # Some scripts accept YYYYMMDD, some accept YYYY-MM-DD; try both (lightweight).
            asof_variants = []
            if remote_concept:
                asof_variants.append(remote_concept.replace("-", ""))
                asof_variants.append(remote_concept)
                asof_variants.append(remote_concept.replace("-", "/"))
            seen = set()
            asof_variants = [x for x in asof_variants if x and (x not in seen and not seen.add(x))]

            last_rc = None
            last_out = ""
            for asof in asof_variants:
                cmd = [sys.executable, sp, "--db", concept_db, "--days", d, "--asof", asof]
                rc, out = run_cmd(cmd)
                last_rc, last_out = rc, out
                logs.append(f"[CONCEPT] rc={rc} days={d} asof={asof}")
                if out:
                    logs.append(out[:2000])
                if rc == 0:
                    break

            if last_rc != 0:
                logs.append("[CONCEPT] warning: concept backfill failed (see logs above).")
        else:
            logs.append("concept backfill script not found -> skip concept daily DB update")

    # -------- ETF backfill (daily close) --------
    if need_etf and remote_etf and etf_db:
        sp = pick_existing_script(SCRIPT_ETF_CANDIDATES)
        if sp:
            d = str(max(days_etf, 5)) if (not force) else "60"
            cmd = [sys.executable, sp, "--db", etf_db, "--days", d, "--asof", remote_etf]
            rc, out = run_cmd(cmd)
            logs.append(f"[ETF] rc={rc} days={d} asof={remote_etf}")
            if out:
                logs.append(out[:2000])
        else:
            logs.append("etf backfill script not found -> skip ETF daily DB update")

    local2 = get_local_last_dates(stock_db, concept_db, etf_db)
    logs.append(f"local_last_dates_after={local2}")

    # final sanity: if still behind remote, note it (does not hard-fail)
    if remote_stock and _is_behind(local2.get("stock"), remote_stock):
        logs.append(f"[WARN] stock DB still behind remote: {local2.get('stock')} < {remote_stock}")
    if remote_stock and _is_behind(local2.get("index"), remote_stock):
        logs.append(f"[WARN] index DB still behind remote: {local2.get('index')} < {remote_stock}")
    if remote_concept and _is_behind(local2.get("concept"), remote_concept):
        logs.append(f"[WARN] concept DB still behind remote: {local2.get('concept')} < {remote_concept}")
    if remote_etf and _is_behind(local2.get("etf"), remote_etf):
        logs.append(f"[WARN] etf DB still behind remote: {local2.get('etf')} < {remote_etf}")

    return local2, logs



def validate_db_latest_day(stock_db: str, concept_db: Optional[str], tz_offset_hours: int = 8) -> Dict[str, Any]:
    """Validate whether the DB has *consistent and plausible* data for the latest target close day.

    This is NOT an external truth oracle; it's an integrity/consistency check to catch:
      - latest date missing in one table
      - required indices missing
      - concept rows too few / duplicated
      - concept amount scale wildly inconsistent with index turnover

    Returns:
      { ok: bool, expected_day: str, issues: [str], details: {...} }
    """
    issues: List[str] = []
    details: Dict[str, Any] = {}

        # expected day should come from interface data availability (daily close endpoints), not only trade calendar
    cal_last = get_source_last_trade_day(tz_offset_hours=int(tz_offset_hours))
    expected_stock_day = get_remote_latest_close_day_stock_indexes() or cal_last
    expected_concept_day = get_remote_latest_close_day_concept(concept_db) or expected_stock_day or cal_last
    # for cross-checks we use a common day that both sources can have (concept day may lag)
    expected_day = min([d for d in [expected_stock_day, expected_concept_day] if d]) if (expected_stock_day or expected_concept_day) else cal_last
    details["calendar_last_trade_day"] = cal_last
    details["expected_stock_day"] = expected_stock_day
    details["expected_concept_day"] = expected_concept_day
    details["expected_day"] = expected_day

    # ---- stock/index DB checks ----
    try:
        con = connect(stock_db)
        if not table_exists(con, "kline_daily"):
            issues.append("stock_db: missing table kline_daily")
            con.close()
            return {"ok": False, "expected_day": expected_day, "issues": issues, "details": details}

        schema = detect_kline_schema_stock(con, "kline_daily")
        if any(schema.get(k) is None for k in ["sec_type", "date", "code"]):
            issues.append(f"stock_db: kline_daily schema incomplete: {schema}")
        else:
            sec_type_col = schema["sec_type"]
            date_col = schema["date"]
            code_col = schema["code"]

            # latest dates
            stock_max = sql_max_date(con, f"SELECT MAX({date_col}) FROM kline_daily WHERE {sec_type_col}='stock'")
            index_max = sql_max_date(
                con,
                f"""SELECT MAX({date_col}) FROM kline_daily
                      WHERE {sec_type_col}='index' AND {code_col} IN (?,?,?,?)""",
                tuple(REQUIRED_INDEX_CODES),
            )
            details["local_stock_max"] = stock_max
            details["local_index_max"] = index_max

            if stock_max and (pd.Timestamp(stock_max) < pd.Timestamp(expected_stock_day)):
                issues.append(f"stock_db: stock daily behind expected_stock_day ({stock_max} < {expected_stock_day})")
            if index_max and (pd.Timestamp(index_max) < pd.Timestamp(expected_stock_day)):
                issues.append(f"stock_db: index daily behind expected_stock_day ({index_max} < {expected_stock_day})")

            # required indices presence on expected_stock_day
            missing = []
            for c in REQUIRED_INDEX_CODES:
                r = con.execute(
                    f"SELECT 1 FROM kline_daily WHERE {sec_type_col}='index' AND {code_col}=? AND substr(replace({date_col},'/','-'),1,10)=? LIMIT 1",
                    (c, expected_stock_day),
                ).fetchone()
                if r is None:
                    missing.append(c)
            if missing:
                issues.append(f"stock_db: required indices missing on {expected_stock_day}: {missing}")
                details["missing_required_indices"] = missing

            # index turnover plausibility (000001+399001)
            try:
                r2 = con.execute(
                    f"""SELECT {code_col} as code, {schema.get('amount')} as amount
                          FROM kline_daily
                          WHERE {sec_type_col}='index' AND {code_col} IN (?,?) AND substr(replace({date_col},'/','-'),1,10)=?""",
                    (CODE_SH000001, CODE_SZ399001, expected_stock_day),
                ).fetchall()
                if r2:
                    raw = pd.Series({str(x[0]): safe_to_float(x[1]) for x in r2})
                    scaled, f = scale_amount_series_to_yuan(raw)
                    mv = float(np.nansum(pd.to_numeric(scaled, errors="coerce").values))
                    details["index_turnover_scale_factor"] = float(f)
                    details["index_turnover_yuan_sum"] = mv
                    # rough sanity window: 10bn to 20tn yuan
                    if not (1e10 <= mv <= 2e13):
                        issues.append(f"stock_db: market turnover (000001+399001) looks abnormal on {expected_stock_day}: {mv:.3g} 元")
                else:
                    issues.append(f"stock_db: cannot read 000001/399001 amount on {expected_stock_day} for plausibility check")
            except Exception as e:
                issues.append(f"stock_db: turnover plausibility check failed: {e}")

        con.close()
    except Exception as e:
        issues.append(f"stock_db validation failed: {e}")

    # ---- concept DB checks ----
    if concept_db and os.path.exists(concept_db):
        try:
            conC = connect(concept_db)
            table = "concept_kline_ths" if table_exists(conC, "concept_kline_ths") else ("concept_kline" if table_exists(conC, "concept_kline") else None)
            if table is None:
                issues.append("concept_db: missing concept_kline_ths / concept_kline table")
                conC.close()
            else:
                schemaC = detect_concept_kline_schema(conC, table)
                if any(schemaC.get(k) is None for k in ["code", "date"]):
                    issues.append(f"concept_db: schema incomplete: {schemaC}")
                else:
                    date_col = schemaC["date"]
                    code_col = schemaC["code"]
                    amt_col = schemaC.get("amount")

                    c_max = sql_max_date(conC, f"SELECT MAX(substr(replace({date_col},'/','-'),1,10)) FROM {table}")
                    details["local_concept_max"] = c_max

                    if c_max and (pd.Timestamp(c_max) < pd.Timestamp(expected_day)):
                        issues.append(f"concept_db: concept daily behind expected_day ({c_max} < {expected_day})")

                    # row count on expected day
                    n = _sql_scalar(conC, f"SELECT COUNT(1) FROM {table} WHERE substr(replace({date_col},'/','-'),1,10)=?", (expected_day,))
                    details["concept_rows_on_expected"] = int(n) if n is not None else None
                    if n is None or int(n) < 800:
                        issues.append(f"concept_db: too few rows on {expected_day} (n={n}), possible bad/partial write")

                    # duplicates on expected day
                    dup = _sql_scalar(
                        conC,
                        f"""SELECT COUNT(1) FROM (
                              SELECT {code_col}, COUNT(1) as c
                              FROM {table}
                              WHERE substr(replace({date_col},'/','-'),1,10)=?
                              GROUP BY {code_col}
                              HAVING c>1
                            )""",
                        (expected_day,),
                    )
                    details["concept_dup_codes_on_expected"] = int(dup) if dup is not None else None
                    if dup is not None and int(dup) > 0:
                        issues.append(f"concept_db: duplicated concept codes on {expected_day} (dup_codes={dup})")

                    # amount sanity vs market turnover if possible
                    if amt_col:
                        max_amt = _sql_scalar(conC, f"SELECT MAX({amt_col}) FROM {table} WHERE substr(replace({date_col},'/','-'),1,10)=?", (expected_day,))
                        med_amt = _sql_scalar(conC, f"SELECT AVG({amt_col}) FROM (SELECT {amt_col} FROM {table} WHERE substr(replace({date_col},'/','-'),1,10)=? ORDER BY {amt_col} LIMIT 1000)", (expected_day,))
                        details["concept_amount_max_raw"] = safe_to_float(max_amt)
                        details["concept_amount_avg_top1000_raw"] = safe_to_float(med_amt)

                        # compare to index turnover if available
                        mv = details.get("index_turnover_yuan_sum", None)
                        if mv is not None and np.isfinite(mv):
                            # assume concept amount is already in yuan; flag if any single concept amount > 2.5x market
                            if max_amt is not None and np.isfinite(safe_to_float(max_amt)) and (safe_to_float(max_amt) > 2.5 * mv):
                                issues.append(f"concept_db: max concept amount seems over-scaled vs market on {expected_day} (max={safe_to_float(max_amt):.3g}, market={mv:.3g})")
                            # also flag if concept amounts are 'too tiny' compared with market (likely unit mismatch)
                            if max_amt is not None and np.isfinite(safe_to_float(max_amt)) and (safe_to_float(max_amt) < mv * 1e-6):
                                issues.append(f"concept_db: concept amounts seem under-scaled vs market on {expected_day} (max={safe_to_float(max_amt):.3g}, market={mv:.3g})")

            conC.close()
        except Exception as e:
            issues.append(f"concept_db validation failed: {e}")

    ok = (len(issues) == 0)
    return {"ok": ok, "expected_day": expected_day, "issues": issues, "details": details}


def _delete_rows_for_trade_date_stock_index(stock_db: str, trade_date: str, codes: Optional[List[str]] = None) -> int:
    """Delete index rows on a given trade_date (best-effort)."""
    try:
        con = connect(stock_db)
        if not table_exists(con, "kline_daily"):
            con.close()
            return 0
        schema = detect_kline_schema_stock(con, "kline_daily")
        if any(schema.get(k) is None for k in ["sec_type", "date", "code"]):
            con.close()
            return 0
        sec_type_col, date_col, code_col = schema["sec_type"], schema["date"], schema["code"]
        if codes:
            ph = ",".join(["?"] * len(codes))
            sql = f"DELETE FROM kline_daily WHERE {sec_type_col}='index' AND substr(replace({date_col},'/','-'),1,10)=? AND {code_col} IN ({ph})"
            params = tuple([trade_date] + list(codes))
        else:
            sql = f"DELETE FROM kline_daily WHERE {sec_type_col}='index' AND substr(replace({date_col},'/','-'),1,10)=?"
            params = (trade_date,)
        cur = con.execute(sql, params)
        con.commit()
        n = int(cur.rowcount) if cur.rowcount is not None else 0
        con.close()
        return n
    except Exception:
        return 0


def _delete_rows_for_trade_date_concept(concept_db: str, trade_date: str) -> int:
    """Delete concept rows on a given trade_date (best-effort)."""
    try:
        con = connect(concept_db)
        table = "concept_kline_ths" if table_exists(con, "concept_kline_ths") else ("concept_kline" if table_exists(con, "concept_kline") else None)
        if table is None:
            con.close()
            return 0
        schema = detect_concept_kline_schema(con, table)
        date_col = schema.get("date") or "trade_date"
        cur = con.execute(f"DELETE FROM {table} WHERE substr(replace({date_col},'/','-'),1,10)=?", (trade_date,))
        con.commit()
        n = int(cur.rowcount) if cur.rowcount is not None else 0
        con.close()
        return n
    except Exception:
        return 0


def repair_latest_trade_day_daily(stock_db: str, concept_db: Optional[str], etf_db: Optional[str], tz_offset_hours: int = 8) -> Tuple[bool, List[str], Dict[str, Any]]:
    """Repair latest trade day data by deleting that day and re-running DAILY backfill scripts.

    This function intentionally uses ONLY daily-close interfaces:
      - baostock backfill script for A-share + indexes
      - adata backfill script for THS concepts
      - optional ETF backfill script
    """
    logs: List[str] = []
    expected_day = get_source_last_trade_day(tz_offset_hours=int(tz_offset_hours))
    logs.append(f"repair_expected_day={expected_day}")

    # Delete latest day (concept + required indices) to avoid 'bad day' lingering
    if concept_db:
        deleted_c = _delete_rows_for_trade_date_concept(concept_db, expected_day)
        logs.append(f"delete_concept_rows({expected_day})={deleted_c}")

    deleted_i = _delete_rows_for_trade_date_stock_index(stock_db, expected_day, codes=list(REQUIRED_INDEX_CODES))
    logs.append(f"delete_index_rows({expected_day})={deleted_i}")

    # Re-run daily backfills (force=True)
    local_after, bflogs = maybe_backfill_all(stock_db, concept_db, etf_db, force=True)
    logs.extend(bflogs[-200:])  # keep recent tail
    logs.append(f"local_after={local_after}")

    val = validate_db_latest_day(stock_db, concept_db, tz_offset_hours=int(tz_offset_hours))
    if not val.get("ok", False):
        logs.append("repair_validate_failed:")
        for s in val.get("issues", [])[:50]:
            logs.append(f"- {s}")
    return bool(val.get("ok", False)), logs, val

def pick_existing_path(candidates: List[str]) -> Optional[str]:
    for p in candidates:
        if os.path.exists(p):
            return p
    return None


def connect(db_path: str) -> sqlite3.Connection:
    con = sqlite3.connect(db_path, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con


def table_exists(con: sqlite3.Connection, table: str) -> bool:
    cur = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
    return cur.fetchone() is not None


def list_tables(con: sqlite3.Connection) -> List[str]:
    cur = con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    return [r[0] for r in cur.fetchall()]


def get_columns(con: sqlite3.Connection, table: str) -> List[str]:
    cur = con.execute(f"PRAGMA table_info('{table}')")
    return [r[1] for r in cur.fetchall()]

def pick_col(cols: List[str], candidates: List[str]) -> Optional[str]:
    """Pick a column name from cols by trying candidates (case-insensitive)."""
    if not cols:
        return None
    lower_map = {c.lower(): c for c in cols}
    for cand in candidates:
        if cand in cols:
            return cand
        c2 = lower_map.get(str(cand).lower())
        if c2:
            return c2
    return None



def df_from_sql(con: sqlite3.Connection, sql: str, params: Tuple = ()) -> pd.DataFrame:
    return pd.read_sql_query(sql, con, params=params)


def safe_to_float(x):
    """Robust numeric parser for sqlite text/number fields.

    Handles:
    - numbers
    - strings with commas ("1,234,567")
    - trailing percent sign ("12.3%")
    - Chinese units: 万 / 亿 / 万亿
    - suffix units: K/M/B
    """
    try:
        if x is None:
            return np.nan
        if isinstance(x, (int, float, np.number)):
            return float(x)
        s = str(x).strip()
        if s == "" or s.lower() in ("none", "nan", "null"):
            return np.nan

        # normalize common formatting
        s = s.replace(",", "").replace(" ", "")
        if s.endswith("%"):
            s = s[:-1]

        mul = 1.0
        # Chinese units (order matters)
        if s.endswith("万亿"):
            mul = 1e12
            s = s[:-2]
        elif s.endswith("亿"):
            mul = 1e8
            s = s[:-1]
        elif s.endswith("万"):
            mul = 1e4
            s = s[:-1]
        # common suffix units
        elif len(s) >= 2 and s[-1] in ("k", "K", "m", "M", "b", "B"):
            suf = s[-1]
            if suf in ("k", "K"):
                mul = 1e3
            elif suf in ("m", "M"):
                mul = 1e6
            elif suf in ("b", "B"):
                mul = 1e9
            s = s[:-1]

        if s == "" or s in ("-", "+"):
            return np.nan
        return float(s) * mul
    except Exception:
        return np.nan

def get_latest_cn_trade_date(today: Optional[pd.Timestamp] = None) -> Optional[pd.Timestamp]:
    """
    Get latest China A-share trading date <= today using akshare trade calendar if available.
    Falls back to weekday heuristic if calendar unavailable.
    """
    try:
        if today is None:
            today = pd.Timestamp(pd.Timestamp.today().date())
        try:
            import akshare as ak  # type: ignore
            if hasattr(ak, "tool_trade_date_hist_sina"):
                cal = ak.tool_trade_date_hist_sina()
                if cal is not None and not cal.empty:
                    col = cal.columns[0]
                    s = pd.to_datetime(cal[col], errors="coerce").dropna()
                    s = s[s <= pd.Timestamp(today.date())]
                    if not s.empty:
                        return pd.Timestamp(s.max().date())
        except Exception:
            pass
        # fallback: if weekend, use last Friday
        d = pd.Timestamp(today.date())
        if d.weekday() >= 5:
            d = d - pd.Timedelta(days=(d.weekday() - 4))
        return d
    except Exception:
        return None


def normalize_date(x) -> Optional[pd.Timestamp]:
    if x is None:
        return None
    if isinstance(x, (pd.Timestamp, dt.datetime, dt.date)):
        return pd.Timestamp(x).normalize()
    s = str(x).strip()
    if s == "":
        return None
    # accept 20260129, 2026-01-29, 2026/1/29
    try:
        if s.isdigit() and len(s) == 8:
            return pd.Timestamp(dt.datetime.strptime(s, "%Y%m%d")).normalize()
    except Exception:
        pass
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M"):
        try:
            return pd.Timestamp(dt.datetime.strptime(s, fmt)).normalize()
        except Exception:
            continue
    try:
        return pd.to_datetime(s).normalize()
    except Exception:
        return None


def digits_code(x: str) -> str:
    if x is None:
        return ""
    s = str(x).strip()
    return "".join(ch for ch in s if ch.isdigit())


def canonical_code(x) -> str:
    """Keep digits and normalize to 6-digit code when possible (index/stock)."""
    s = digits_code(x)
    if s == "":
        return ""
    # if looks like numeric code, zfill to 6
    return s.zfill(6)



def zscore(s: pd.Series, window: int = 20, min_periods: int = 5) -> pd.Series:
    """Rolling z-score (uses a smaller min_periods to avoid long leading NaNs).

    If rolling std becomes 0, this returns NaN for those points.
    """
    mu = s.rolling(window, min_periods=min_periods).mean()
    sd = s.rolling(window, min_periods=min_periods).std(ddof=0)
    sd0 = sd == 0
    z = (s - mu) / sd.replace(0, np.nan)
    # when sd==0, treat z as 0 (no deviation)
    z = z.where(~sd0, 0.0)
    return z

def robust_zscore(
    s: pd.Series,
    window: int = 60,
    min_periods: int = 10,
    clip: float = 4.0,
    sigma_floor: float = 1e-6,
) -> pd.Series:
    """Rolling robust z-score using median/MAD (right-aligned window).

    This is more stable than mean/std on small samples and in the presence of outliers.
    For each t, z_t = (x_t - median(window)) / (1.4826 * MAD(window)),
    where MAD(window)=median(|x_i - median(window)|).

    Notes:
      - Uses rolling.apply; dataset is small (<= a few hundred rows), so performance is fine.
      - Applies symmetric clipping to reduce distortion from bad data points.
    """
    def _rz(x: np.ndarray) -> float:
        # x is the window values (raw=True)
        if x.size == 0 or np.all(np.isnan(x)):
            return np.nan
        med = np.nanmedian(x)
        dev = np.abs(x - med)
        mad = np.nanmedian(dev)
        sigma = 1.4826 * mad
        if not np.isfinite(sigma) or sigma < sigma_floor:
            sigma = sigma_floor
        z = (x[-1] - med) / sigma
        if np.isfinite(z):
            if clip is not None:
                z = float(np.clip(z, -clip, clip))
            return float(z)
        return np.nan

    return s.rolling(window, min_periods=min_periods).apply(_rz, raw=True)


def sigmoid_0_100(x: float, scale: float) -> float:
    """Map a (rough) standard-score into 0~100 using a logistic function.

    scale controls the slope; for x~N(0, scale^2), this yields a stable percentile-like score.
    """
    if not (np.isfinite(x) and np.isfinite(scale)) or scale <= 0:
        return np.nan
    t = float(np.clip(x / scale, -20.0, 20.0))
    return float(100.0 / (1.0 + np.exp(-t)))


def weights_scale(weights: Dict[str, float], keys: List[str]) -> float:
    """Compute a stable scale parameter from weights (independent of sample window).

    We follow the current normalization in comp (divide by sum(abs(w_k))).
    If z_k are roughly unit-scale, comp's std is approx sqrt(sum(w_norm^2)).
    """
    w = [float(weights.get(k, 0.0)) for k in keys if float(weights.get(k, 0.0)) != 0.0]
    if not w:
        return np.nan
    sumabs = float(np.sum(np.abs(w)))
    if sumabs <= 0:
        return np.nan
    w_norm = [wi / sumabs for wi in w]
    return float(math.sqrt(float(np.sum(np.square(w_norm)))))



def ema(s: pd.Series, span: int) -> pd.Series:
    return s.ewm(span=span, adjust=False).mean()


def minmax_0_100(x: pd.Series) -> pd.Series:
    """Scale a series to 0..100 using historical min/max.

    If there is only one (or all equal) finite value, returns 50 for those finite points.
    """
    out = pd.Series(index=x.index, dtype=float)
    mask = np.isfinite(x)
    if not mask.any():
        return out
    vals = x[mask].values.astype(float)
    lo = float(np.nanmin(vals))
    hi = float(np.nanmax(vals))
    if not np.isfinite(lo) or not np.isfinite(hi):
        return out
    if hi == lo:
        out.loc[mask] = 50.0
        return out
    out.loc[mask] = (x.loc[mask] - lo) / (hi - lo) * 100.0
    return out


def last_n_trade_dates(con: sqlite3.Connection, table: str, date_col: str, n: int = 60) -> List[pd.Timestamp]:
    df = df_from_sql(con, f"SELECT DISTINCT {date_col} as d FROM {table} ORDER BY {date_col} DESC LIMIT ?", (n,))
    dates = []
    for v in df["d"].tolist():
        t = normalize_date(v)
        if t is not None:
            dates.append(t)
    return sorted(set(dates))


# ----------------------------
# Stock/index schema + load
# ----------------------------
def detect_kline_schema_stock(con: sqlite3.Connection, table: str = "kline_daily") -> Dict[str, str]:
    cols = get_columns(con, table)

    def pick(cands):
        return pick_col(cols, list(cands))

    return {
        "pct": pick(["change_pct", "pct_chg", "pct", "changePercent", "PCT_CHG", "涨跌幅"]),
        "amount": pick(["amount", "turnover", "amt", "AMOUNT", "TURNOVER", "成交额", "成交金额"]),
        "code": pick(["sec_code", "code", "stock_code", "SECUCODE", "SEC_CODE"]),
        "sec_type": pick(["sec_type", "type", "SEC_TYPE"]),
        "date": pick(["trade_date", "date", "dt", "TRADE_DATE", "DATE"]),
        "close": pick(["close", "c", "CLOSE"]),
    }


def load_stock_cross_section(con: sqlite3.Connection, dates: List[pd.Timestamp]) -> pd.DataFrame:
    schema = detect_kline_schema_stock(con)
    if any(v is None for v in schema.values()):
        raise RuntimeError(f"kline_daily schema missing columns: {schema}")

    date_strs = [d.strftime("%Y-%m-%d") for d in dates]
    placeholders = ",".join(["?"] * len(date_strs))
    sql = f"""
    SELECT
      {schema['sec_type']} as sec_type,
      {schema['code']} as sec_code,
      {schema['date']} as trade_date_raw,
      {schema['pct']} as change_pct,
      {schema['amount']} as amount,
      {schema['close']} as close
    FROM kline_daily
    WHERE substr(replace({schema['date']}, '/', '-'),1,10) IN ({placeholders})
      AND {schema['sec_type']} = 'stock'
    """
    df = df_from_sql(con, sql, tuple(date_strs))
    df["trade_date"] = df["trade_date_raw"].apply(normalize_date)
    df["sec_code"] = df["sec_code"].apply(canonical_code)
    df["change_pct"] = df["change_pct"].apply(safe_to_float)
    df["amount"] = df["amount"].apply(safe_to_float)
    df["close"] = df["close"].apply(safe_to_float)
    return df.dropna(subset=["trade_date", "sec_code"])


def load_index_series(con: sqlite3.Connection, codes: List[str], dates: List[pd.Timestamp]) -> pd.DataFrame:
    schema = detect_kline_schema_stock(con)
    if any(v is None for v in schema.values()):
        raise RuntimeError(f"kline_daily schema missing columns: {schema}")

    date_strs = [d.strftime("%Y-%m-%d") for d in dates]
    placeholders = ",".join(["?"] * len(date_strs))
    sql = f"""
    SELECT
      {schema['sec_type']} as sec_type,
      {schema['code']} as sec_code_raw,
      {schema['date']} as trade_date_raw,
      {schema['pct']} as change_pct,
      {schema['amount']} as amount,
      {schema['close']} as close
    FROM kline_daily
    WHERE substr(replace({schema['date']}, '/', '-'),1,10) IN ({placeholders})
      AND {schema['sec_type']} = 'index'
    """
    df = df_from_sql(con, sql, tuple(date_strs))
    if df.empty:
        return df
    df["trade_date"] = df["trade_date_raw"].apply(normalize_date)
    df["sec_code"] = df["sec_code_raw"].apply(canonical_code)
    df["change_pct"] = df["change_pct"].apply(safe_to_float)
    df["amount"] = df["amount"].apply(safe_to_float)
    df["close"] = df["close"].apply(safe_to_float)

    need = set(digits_code(c) for c in codes)
    df = df[df["sec_code"].isin(need)]
    return df.dropna(subset=["trade_date", "sec_code"])


def compute_market_turnover(index_df: pd.DataFrame) -> pd.Series:
    """market turnover = amount(sh000001)+amount(sz399001) per date"""
    if index_df is None or index_df.empty:
        return pd.Series(dtype=float)
    df = index_df[index_df["sec_code"].isin([CODE_SH000001, CODE_SZ399001])].copy()
    if df.empty:
        return pd.Series(dtype=float)
    return df.groupby("trade_date")["amount"].sum().sort_index()


def infer_amount_scale_to_yuan_with_target(
    median_value: float,
    target_center: float,
    target_low: float,
    target_high: float,
) -> float:
    """Infer a scale factor to convert an 'amount' value into RMB yuan.

    Different sources use different units (yuan / 千元 / 万元 / 亿元).
    We pick a multiplicative factor among common candidates.
    """
    try:
        v = float(median_value)
    except Exception:
        return 1.0
    if not np.isfinite(v) or v <= 0:
        return 1.0

    candidates = [1.0, 1e2, 1e3, 1e4, 1e5, 1e6, 1e8]
    best = 1.0
    best_score = float("inf")
    for f in candidates:
        x = v * f
        score = abs(np.log10(x) - np.log10(target_center))
        if x < target_low or x > target_high:
            score += 5.0
        if score < best_score:
            best_score = score
            best = f
    return float(best)


def scale_amount_series_to_yuan(
    s: pd.Series,
    target_center: float = 1e12,
    target_low: float = 1e10,
    target_high: float = 1e13,
) -> Tuple[pd.Series, float]:
    """Scale an amount series into RMB yuan using median-based inference."""
    if s is None or len(s) == 0:
        return s, 1.0
    ss = pd.to_numeric(s, errors="coerce")
    clean = ss.replace([np.inf, -np.inf], np.nan).dropna()
    med = float(clean.median()) if len(clean) else float("nan")
    f = infer_amount_scale_to_yuan_with_target(med, target_center, target_low, target_high) if np.isfinite(med) and med > 0 else 1.0
    return ss * f, f






def build_market_turnover(
    index_df: pd.DataFrame,
    stock_df: pd.DataFrame,
    dates: List[pd.Timestamp],
    msgs: List[str],
) -> pd.Series:
    """Build a robust market turnover(amount) series for the dashboard.

    Priority (per your spec):
      1) index turnover = amount(000001) + amount(399001)
      2) fallback to stock turnover sum if index amount missing

    Fixes:
      - treat <=0 as missing
      - detect index/stock unit mismatch and rescale index if necessary
      - repair obvious outliers using rolling median (market turnover should not jump 5x day-to-day)
    """
    idx_amt = pd.Series(index=pd.Index(dates, name="trade_date"), dtype=float)
    stk_amt = pd.Series(index=pd.Index(dates, name="trade_date"), dtype=float)

    # --- index turnover (preferred) ---
    if index_df is not None and not index_df.empty and "amount" in index_df.columns:
        t = index_df.copy()
        if "trade_date" in t.columns:
            t["trade_date"] = t["trade_date"].apply(normalize_date)
        t["sec_code"] = t["sec_code"].apply(canonical_code)
        t["amount"] = t["amount"].apply(safe_to_float)
        t = t.dropna(subset=["trade_date", "sec_code"])
        sub = t[t["sec_code"].isin([CODE_SH000001, CODE_SZ399001])][["trade_date", "sec_code", "amount"]]
        if not sub.empty:
            piv = sub.pivot_table(index="trade_date", columns="sec_code", values="amount", aggfunc="last")
            idx_amt = (piv.get(CODE_SH000001) + piv.get(CODE_SZ399001)).reindex(dates).astype(float)

    idx_amt = idx_amt.where(idx_amt > 0)

    # --- stock turnover sum (fallback) ---
    if stock_df is not None and not stock_df.empty and "amount" in stock_df.columns:
        s = stock_df.copy()
        if "trade_date" in s.columns:
            s["trade_date"] = s["trade_date"].apply(normalize_date)
        s["amount"] = s["amount"].apply(safe_to_float)
        s = s.dropna(subset=["trade_date"])
        stk_amt = s.groupby("trade_date")["amount"].sum().reindex(dates).astype(float)

    stk_amt = stk_amt.where(stk_amt > 0)

    # --- scale amount units to RMB yuan (inferred) ---
    idx_amt, f_idx = scale_amount_series_to_yuan(idx_amt)
    stk_amt, f_stk = scale_amount_series_to_yuan(stk_amt)
    if f_idx != 1.0:
        msgs.append(f"指数成交额单位推断：已乘以 {f_idx:g} 转为元口径")
    if f_stk != 1.0:
        msgs.append(f"全A成交额单位推断：已乘以 {f_stk:g} 转为元口径")


    # --- choose base series (per spec: ALWAYS prefer index sum 000001+399001) ---
    base = idx_amt.copy()
    fallback = stk_amt
    if base.notna().sum() == 0 and fallback.notna().sum() > 0:
        # only when index turnover is entirely unavailable
        base = fallback.copy()
        fallback = idx_amt
        msgs.append("指数成交额不可用：暂用全A股票成交额汇总作为市场成交额口径（仅为兜底）")
    # --- patch missing days using fallback ---
    miss = base.isna() | (base <= 0)
    if miss.any() and fallback is not None:
        base.loc[miss] = fallback.loc[miss]

    # --- repair outliers using rolling median ---
    # market turnover shouldn't jump 5x day-to-day in a normal regime; treat as data glitch
    med = base.rolling(7, min_periods=3).median()
    ratio = base / med
    glitch = ratio.where(np.isfinite(ratio)).apply(lambda x: False)  # placeholder
    try:
        glitch = (ratio > 5.0) | (ratio < 0.2)
    except Exception:
        glitch = pd.Series(False, index=base.index)

    if glitch.any():
        base.loc[glitch] = med.loc[glitch]
        msgs.append("市场成交额检测到疑似脏数据跳变：已用7日滚动中位数修复部分异常日")

    return base



def detect_etf_schema(con: sqlite3.Connection) -> Tuple[str, str, List[str]]:
    '''
    Detect ETF share-total table schema.

    Returns:
        table_name, date_col, cols
    '''
    candidates = ["etf_total", "etf_share_total", "etf_equity_total", "etf_total_share", "etf_shares"]
    existing_tables = set(
        r[0]
        for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        if r and r[0]
    )

    table = None
    for t in candidates:
        if t in existing_tables:
            table = t
            break
    if table is None:
        # fallback: any table containing 'etf' and 'total/share'
        for t in sorted(existing_tables):
            tl = t.lower()
            if "etf" in tl and ("total" in tl or "share" in tl):
                table = t
                break
    if table is None:
        raise RuntimeError(
            f"No ETF total/share table found in db. Existing tables: {sorted(existing_tables)[:30]}"
        )

    cols = get_columns(con, table)
    cols_l = [c.lower() for c in cols]

    # date column detection
    date_col = None
    for cand in ["trade_date", "date", "dt"]:
        if cand in cols_l:
            date_col = cols[cols_l.index(cand)]
            break
    if date_col is None:
        for i, c in enumerate(cols_l):
            if "date" in c:
                date_col = cols[i]
                break
    if date_col is None:
        raise RuntimeError(f"ETF table '{table}' has no recognizable date column. cols={cols}")

    return table, date_col, cols

def load_etf_total_df(etf_db_path: str) -> pd.DataFrame:
    """Load ETF equity share totals from local DB.

    Expect table like etf_total with columns:
      trade_date, sse_share_total, szse_share_total, total_share, updated_at

    Also supports older schemas by detecting usable columns.

    Returns columns:
      trade_date, sse_share_total, szse_share_total, total_share
    """
    cols_out = ["trade_date", "sse_share_total", "szse_share_total", "total_share"]
    if not etf_db_path or not os.path.exists(etf_db_path):
        return pd.DataFrame(columns=cols_out)

    con = connect(etf_db_path)
    try:
        table, date_col, cols = detect_etf_schema(con)

        # Prefer explicit sse/szse/total if present (case-insensitive)
        sse = pick_col(cols, ["sse_share_total", "share_sse", "sse_share", "SSE_SHARE_TOTAL"]) 
        szse = pick_col(cols, ["szse_share_total", "share_szse", "szse_share", "SZSE_SHARE_TOTAL"]) 
        total = pick_col(cols, ["total_share", "share_total", "share", "total", "shares", "TOTAL_SHARE"]) 
        sel = [f"{date_col} as d"]
        if sse:
            sel.append(f"{sse} as sse_share_total")
        else:
            sel.append("NULL as sse_share_total")
        if szse:
            sel.append(f"{szse} as szse_share_total")
        else:
            sel.append("NULL as szse_share_total")
        if total:
            sel.append(f"{total} as total_share")
        else:
            # if no total column, compute from sse/szse (but do NOT treat missing as 0 here)
            if sse and szse:
                sel.append(f"({sse}+{szse}) as total_share")
            elif sse:
                sel.append(f"{sse} as total_share")
            elif szse:
                sel.append(f"{szse} as total_share")
            else:
                raise RuntimeError(f"ETF table has no recognizable share columns: {cols}")

        sql = f"SELECT {', '.join(sel)} FROM {table} ORDER BY {date_col}"
        df = df_from_sql(con, sql)

        df["trade_date"] = df["d"].apply(normalize_date)
        for c in ["sse_share_total", "szse_share_total", "total_share"]:
            df[c] = df[c].apply(safe_to_float)

        df = df.dropna(subset=["trade_date"]).sort_values("trade_date")
        df = df.drop_duplicates(subset=["trade_date"], keep="last")

        return df[cols_out]
    finally:
        con.close()


def compute_etf_share_pct(etf_df: pd.DataFrame, dates: List[pd.Timestamp]) -> Tuple[pd.Series, List[str]]:
    """Compute ETF share daily pct with fallback logic (treat 0 as missing)."""
    msgs: List[str] = []
    if etf_df is None or etf_df.empty:
        return pd.Series(index=dates, dtype=float), ["etf_total empty (cannot compute etf share pct)"]

    df = etf_df.copy()
    df = df.dropna(subset=["trade_date"]).sort_values("trade_date")
    df = df.set_index("trade_date")

    # treat <=0 as missing
    for c in ["sse_share_total", "szse_share_total", "total_share"]:
        if c in df.columns:
            df.loc[df[c] <= 0, c] = np.nan

    pct = pd.Series(index=dates, dtype=float)

    # use available dates in df (may be more than panel dates)
    all_dates = sorted(df.index.unique())
    prev_map = {d: all_dates[i-1] if i > 0 else None for i, d in enumerate(all_dates)}

    def pct_one(d):
        prev = prev_map.get(d)
        if prev is None:
            return np.nan
        sse_t, sz_t = df.at[d, "sse_share_total"] if "sse_share_total" in df.columns else np.nan, df.at[d, "szse_share_total"] if "szse_share_total" in df.columns else np.nan
        sse_p, sz_p = df.at[prev, "sse_share_total"] if "sse_share_total" in df.columns else np.nan, df.at[prev, "szse_share_total"] if "szse_share_total" in df.columns else np.nan

        # Prefer total when both markets are present (and prev present)
        if np.isfinite(sse_t) and np.isfinite(sz_t) and np.isfinite(sse_p) and np.isfinite(sz_p):
            return (sse_t + sz_t) / (sse_p + sz_p) - 1.0

        # Fallback to single market
        if np.isfinite(sse_t) and np.isfinite(sse_p):
            return sse_t / sse_p - 1.0
        if np.isfinite(sz_t) and np.isfinite(sz_p):
            return sz_t / sz_p - 1.0

        return np.nan

    # compute on df dates then reindex to panel dates
    pct_df = pd.Series({d: pct_one(d) for d in all_dates}, dtype=float)
    pct = pct_df.reindex(dates)

    if pct.isna().all():
        msgs.append("etf share pct all missing (likely data gaps or zeros treated as missing)")
    return pct, msgs


# ----------------------------
# Sentiment computation
# ----------------------------
def compute_daily_sentiment(
    stock_df: pd.DataFrame,
    index_df: pd.DataFrame,
    etf_df: pd.DataFrame,
    dates: List[pd.Timestamp],
    weights: Dict[str, float],
    hot_top_n: int = 100,
) -> Tuple[pd.DataFrame, List[str]]:
    """Compute daily sentiment metrics from local DB data.

    Ensures the panel can output numeric values even when history is short
    (rolling stats use min_periods=5 and composite renormalizes weights per-day
    based on available factors).
    """

    msgs: List[str] = []
    if stock_df is None or stock_df.empty:
        return pd.DataFrame(), ["stock_df empty (no stock cross-section in DB for selected dates)"]

    df = stock_df.copy()
    df = df.dropna(subset=["trade_date", "change_pct", "amount"]).copy()
    df["trade_date"] = df["trade_date"].apply(normalize_date)
    df["change_pct"] = df["change_pct"].apply(safe_to_float)
    df["amount"] = df["amount"].apply(safe_to_float)

    df = df.dropna(subset=["trade_date"]).copy()
    df["is_flat"] = np.isclose(df["change_pct"].fillna(0), 0.0)

    g = df.groupby("trade_date")
    allA_ew_pct = g["change_pct"].mean()  # unit: percent
    allA_ew_ret = (allA_ew_pct / 100.0).reindex(dates)

    non_flat = df[~df["is_flat"]]
    g2 = non_flat.groupby("trade_date")
    up_cnt = g2["change_pct"].apply(lambda s: int((s > 0).sum()))
    down_cnt = g2["change_pct"].apply(lambda s: int((s < 0).sum()))
    denom = (up_cnt + down_cnt).replace(0, np.nan)
    up_ratio = (up_cnt / denom).reindex(dates)
    up_down_ratio = (up_cnt / down_cnt.replace(0, np.nan)).reindex(dates)

    # hot proxy: daily top N by amount
    def hot_mean(gdf: pd.DataFrame) -> float:
        sub = gdf.nlargest(int(hot_top_n), "amount")
        return float(sub["change_pct"].mean()) if len(sub) else np.nan

    try:
        hot_ret_pct = df.groupby("trade_date")[["amount","change_pct"]].apply(hot_mean, include_groups=False).reindex(dates)  # percent
    except TypeError:
        hot_ret_pct = df.groupby("trade_date")[["amount","change_pct"]].apply(hot_mean).reindex(dates)  # percent
    hot_excess = ((hot_ret_pct - allA_ew_pct.reindex(dates)) / 100.0).reindex(dates)

    # style: zz1000 - hs300 (decimal returns)
    rel = pd.Series(index=dates, dtype=float)
    if index_df is not None and not index_df.empty:
        idx = index_df.copy()
        idx["trade_date"] = idx["trade_date"].apply(normalize_date)
        idx["sec_code"] = idx["sec_code"].apply(canonical_code)
        idx["change_pct"] = idx["change_pct"].apply(safe_to_float)
        idx["close"] = idx.get("close", np.nan)
        idx["close"] = idx["close"].apply(safe_to_float)

        def ret_series(code: str) -> pd.Series:
            sub = idx[idx["sec_code"] == code].dropna(subset=["trade_date"]).sort_values("trade_date")
            if sub.empty:
                return pd.Series(dtype=float)
            sub = sub.set_index("trade_date")
            if sub["change_pct"].notna().any():
                return (sub["change_pct"] / 100.0).astype(float)
            if sub["close"].notna().any():
                return sub["close"].pct_change().astype(float)
            return pd.Series(dtype=float)

        hs = ret_series(CODE_HS300).reindex(dates)
        zz = ret_series(CODE_ZZ1000).reindex(dates)
        if hs.isna().all() or zz.isna().all():
            msgs.append("HS300/ZZ1000 日收益缺失：无法计算 小盘-大盘相对(1000-300)")
        rel = (zz - hs)
    else:
        msgs.append("index_df empty: cannot compute 小盘-大盘相对(1000-300)")

    # market turnover (amount): prefer stock turnover; patch with index turnover; repair bad zeros/outliers
    mkt_amt = build_market_turnover(index_df=index_df, stock_df=df, dates=dates, msgs=msgs)

    ma10 = mkt_amt.rolling(10, min_periods=5).mean()
    turnover_rel_ma10 = (mkt_amt / ma10) - 1.0
    # extra guard: avoid extreme artifacts when rolling mean is missing/too small
    turnover_rel_ma10 = turnover_rel_ma10.where(np.isfinite(turnover_rel_ma10))
    clipped = turnover_rel_ma10.clip(lower=-0.90, upper=3.00)
    if (clipped != turnover_rel_ma10).fillna(False).any():
        msgs.append("成交额/MA10 指标检测到异常极值：已做[-90%, +300%]裁剪以避免口径错误/缺数据导致的失真")
    turnover_rel_ma10 = clipped

    # ETF share pct with fallback logic
    etf_pct, etf_msgs = compute_etf_share_pct(etf_df, dates)
    msgs.extend(etf_msgs)

    out = pd.DataFrame(index=dates)
    out.index.name = "trade_date"
    out["allA_ew_ret"] = allA_ew_ret
    out["up_cnt"] = up_cnt.reindex(dates)
    out["down_cnt"] = down_cnt.reindex(dates)
    out["up_ratio"] = up_ratio
    out["up_down_ratio"] = up_down_ratio
    out["hot_excess"] = hot_excess
    out["rel_zz1000_hs300"] = rel.reindex(dates)
    out["etf_eq_share_pct"] = etf_pct.reindex(dates)
    out["turnover_rel_ma10"] = turnover_rel_ma10.reindex(dates)

    # composite sentiment score (raw weighted z; no percentile mapping)
    zf = pd.DataFrame(index=out.index)
    # Standardize factors with a robust z-score (median/MAD) on a longer window to reduce small-sample drift
    zf["B"] = robust_zscore(out["up_ratio"], window=60, min_periods=10)
    zf["L"] = robust_zscore(out["turnover_rel_ma10"], window=60, min_periods=10)
    zf["H"] = robust_zscore(out["hot_excess"], window=60, min_periods=10)
    zf["R"] = robust_zscore(out["rel_zz1000_hs300"], window=60, min_periods=10)
    zf["E"] = robust_zscore(out["etf_eq_share_pct"], window=60, min_periods=10)

    keys = [k for k in ["B", "L", "H", "R", "E"] if k in weights]
    if not keys:
        msgs.append("weights 未配置(B/L/H/R/E)，sentiment_score 无法计算")
        out["sentiment_score"] = np.nan
    else:
        w_raw = {k: float(weights.get(k, 0.0)) for k in keys}
        comp = []
        for d in out.index:
            num = 0.0
            den = 0.0
            for k in keys:
                v = zf.at[d, k]
                wv = w_raw.get(k, 0.0)
                if np.isfinite(v) and np.isfinite(wv) and (wv != 0):
                    num += float(wv) * float(v)
                    den += float(wv) if float(wv) > 0 else abs(float(wv))
            comp.append(num / den if den > 0 else np.nan)
        comp_s = pd.Series(comp, index=out.index, dtype=float)
        scale = weights_scale(w_raw, keys)
        if np.isfinite(scale) and scale > 0:
            t = np.clip(comp_s / scale, -20.0, 20.0)
            out["sentiment_score"] = 100.0 / (1.0 + np.exp(-t))
        else:
            msgs.append("weights_scale 无效，sentiment_score 无法映射到0-100")
            out["sentiment_score"] = np.nan

    out["sentiment_mom"] = ema(out["sentiment_score"], 3) - ema(out["sentiment_score"], 10)
    out["sentiment_z"] = zscore(out["sentiment_mom"], 20, 5)

    return out.reset_index(), msgs


# ----------------------------
# Concepts (ths)
# ----------------------------
def detect_concept_kline_schema(con: sqlite3.Connection, table: str = "concept_kline_ths") -> Dict[str, str]:
    cols = get_columns(con, table)

    def pick(cands):
        return pick_col(cols, list(cands))

    return {
        "code": pick(["concept_code", "sec_code", "index_code", "INDEX_CODE", "CONCEPT_CODE"]),
        "date": pick(["trade_date", "date", "dt", "TRADE_DATE", "DATE"]),
        "close": pick(["close"]),
        "pre_close": pick(["pre_close", "preClose"]),
        "pct": pick(["change_pct", "pct_chg", "pct", "PCT_CHG", "涨跌幅"]),
        "amount": pick(["amount", "turnover", "amt", "AMOUNT", "TURNOVER", "成交额", "成交金额"]),
        "volume": pick(["volume", "vol"]),
    }


def load_concept_kline(con: sqlite3.Connection, dates: List[pd.Timestamp], table: str = "concept_kline_ths") -> pd.DataFrame:
    schema = detect_concept_kline_schema(con, table)
    if schema["code"] is None or schema["date"] is None:
        raise RuntimeError(f"concept_kline_ths schema missing columns: {schema}")

    date_strs = [d.strftime("%Y-%m-%d") for d in dates]
    placeholders = ",".join(["?"] * len(date_strs))
    sql = f"""
    SELECT
      {schema['code']} as concept_code_raw,
      {schema['date']} as trade_date_raw,
      {schema.get('close','NULL')} as close,
      {schema.get('pre_close','NULL')} as pre_close,
      {schema.get('pct','NULL')} as change_pct,
      {schema.get('amount','NULL')} as amount,
      {schema.get('volume','NULL')} as volume
    FROM {table}
    WHERE substr(replace({schema['date']}, '/', '-'),1,10) IN ({placeholders})
    """
    df = df_from_sql(con, sql, tuple(date_strs))
    if df.empty:
        return df

    df["trade_date"] = df["trade_date_raw"].apply(normalize_date)
    df["concept_code"] = df["concept_code_raw"].apply(canonical_code)
    df["change_pct"] = df["change_pct"].apply(safe_to_float)
    df["amount"] = df["amount"].apply(safe_to_float)
    df["close"] = df["close"].apply(safe_to_float)
    df["pre_close"] = df["pre_close"].apply(safe_to_float)
    return df.dropna(subset=["trade_date", "concept_code"])



def load_concept_name_map(con: sqlite3.Connection) -> Dict[str, str]:
    """Load concept code -> name mapping.

    Required by user: use concept_master(index_code, name).
    Falls back to heuristic search if concept_master not present.
    """
    tables = list_tables(con)

    # strict: concept_master(index_code, name)
    if "concept_master" in tables:
        cols = get_columns(con, "concept_master")
        if "index_code" in cols and "name" in cols:
            df = df_from_sql(con, "SELECT index_code as code, name as name FROM concept_master")
            df["code"] = df["code"].apply(canonical_code)
            df["name"] = df["name"].astype(str)
            mp = dict(zip(df["code"], df["name"]))
            if mp:
                # apply filtered_concept.csv exclusion (if present)
                filtered = read_filtered_concepts_csv()
                if filtered:
                    for k in list(mp.keys()):
                        if str(k) in filtered:
                            mp.pop(k, None)
                return mp

    # fallback: search other tables (legacy)
    candidates = ["ths_concept_list", "concept_list", "concept_index_map", "concept_map", "concept_meta"]
    for t in tables:
        if t not in candidates and ("concept" in t and ("list" in t or "map" in t or "meta" in t or "master" in t)):
            candidates.append(t)

    for t in candidates:
        if t not in tables:
            continue
        cols = get_columns(con, t)
        name_col = next((c for c in ["name", "concept_name", "sec_name"] if c in cols), None)
        code_col = next((c for c in ["concept_code", "index_code", "sec_code", "code"] if c in cols), None)
        if not (name_col and code_col):
            continue
        df = df_from_sql(con, f"SELECT {code_col} as code, {name_col} as name FROM {t}")
        if df.empty:
            continue
        df["code"] = df["code"].apply(canonical_code)
        df["name"] = df["name"].astype(str)
        mp = dict(zip(df["code"], df["name"]))
        if mp:
            return mp

    return {}



def compute_theme_panel(
    concept_df: pd.DataFrame,
    name_map: Dict[str, str],
    market_turnover: pd.Series,
    latest_date: pd.Timestamp,
    top_k: int = 30,
):
    """Theme panel.

    Requirements:
    - Display concept_code + concept_name(from concept_master(index_code/name))
    - Temperature（0-100） and Risk（0-100） must NOT be blank (fill with median when history is insufficient)
    - Show ret_1d, ret_5d, amount_share (as % of market turnover today)
    - Do NOT show concept raw amount.
    """
    msgs: List[str] = []
    if concept_df is None or concept_df.empty:
        return pd.DataFrame(), ["concept_df empty for selected dates"]

    df = concept_df.copy()
    df["trade_date"] = df["trade_date"].apply(normalize_date)
    df = df.dropna(subset=["trade_date", "concept_code"]).sort_values(["concept_code", "trade_date"])
    df["concept_code"] = df["concept_code"].apply(canonical_code)

    df["close"] = df["close"].apply(safe_to_float)
    df["amount"] = df["amount"].apply(safe_to_float)
    df["change_pct"] = df["change_pct"].apply(safe_to_float) / 100.0

    # feature engineering (vectorized; avoids pandas groupby.apply deprecation warnings)
    df = df.sort_values(["concept_code", "trade_date"]).copy()
    df["ret_1d"] = df["change_pct"]
    df["ret_5d"] = df.groupby("concept_code")["close"].pct_change(5)
    df["runup10"] = df.groupby("concept_code")["close"].pct_change(10)
    df["vol10"] = (
        df.groupby("concept_code")["ret_1d"]
        .rolling(10, min_periods=5)
        .std(ddof=0)
        .reset_index(level=0, drop=True)
    )


    
    # extension / drawdown features
    df["ma10_close"] = (
        df.groupby("concept_code")["close"]
        .rolling(10, min_periods=5)
        .mean()
        .reset_index(level=0, drop=True)
    )
    df["ext10"] = (df["close"] / df["ma10_close"]) - 1.0
    df["max10_close"] = (
        df.groupby("concept_code")["close"]
        .rolling(10, min_periods=5)
        .max()
        .reset_index(level=0, drop=True)
    )
    df["dd10"] = (df["close"] / df["max10_close"]) - 1.0
    df["abs_dd10"] = df["dd10"].abs()

# amount share (today)
    if market_turnover is None or market_turnover.empty:
        msgs.append("market_turnover empty: cannot compute concept amount share")
        df["amount_share"] = np.nan
    else:
        df["market_amount"] = df["trade_date"].map(market_turnover)
        df["market_amount"] = df["market_amount"].apply(safe_to_float)
        df.loc[df["market_amount"] <= 0, "market_amount"] = np.nan
        # concept amount unit -> yuan (calibrated by share vs market_amount)
        concept_amt = df["amount"].copy()

        # Infer a scale factor using latest_date cross-section and market_amount.
        # Goal: pick a common multiplier among [1, 1e2, 1e3, 1e4, 1e5, 1e6, 1e8]
        # that makes 'concept_amt / market_amount' fall into a plausible range.
        f_c = 1.0
        try:
            sub = df[df["trade_date"] == latest_date][["amount", "market_amount"]].copy()
            sub["amount"] = pd.to_numeric(sub["amount"], errors="coerce")
            sub["market_amount"] = pd.to_numeric(sub["market_amount"], errors="coerce")
            sub = sub.replace([np.inf, -np.inf], np.nan).dropna()
            if not sub.empty and float(sub["market_amount"].median()) > 0:
                candidates = [1.0, 1e2, 1e3, 1e4, 1e5, 1e6, 1e8]
                target_med_share = 0.003  # ~0.3% median share as a loose anchor
                best_f = 1.0
                best_score = float("inf")
                for f in candidates:
                    share = (sub["amount"] * f) / sub["market_amount"]
                    share = share.replace([np.inf, -np.inf], np.nan).dropna()
                    if share.empty:
                        continue
                    med_s = float(share.median())
                    p95 = float(share.quantile(0.95))
                    mx = float(share.max())
                    penalty = 0.0
                    # hard constraints: no single concept should exceed market turnover
                    if mx > 1.2:
                        penalty += 20.0
                    # very extreme tail likely indicates unit mismatch
                    if p95 > 0.3:
                        penalty += 5.0
                    # plausible median share band (very loose)
                    if (med_s < 1e-4) or (med_s > 0.05):
                        penalty += 5.0
                    # keep med share finite and positive
                    if not (1e-8 < med_s < 0.2):
                        penalty += 10.0
                    score = abs(np.log(max(med_s, 1e-12)) - np.log(target_med_share)) + penalty
                    if score < best_score:
                        best_score = score
                        best_f = f
                f_c = float(best_f)
                if f_c != 1.0:
                    msgs.append(f"概念成交额单位推断：按'成交额占市场比例'校准，已乘以 {f_c:g} 转为元口径")
        except Exception:
            pass

        if f_c != 1.0:
            concept_amt = concept_amt * f_c

        df["amount_share"] = concept_amt / df["market_amount"]

    
    # Δamt_share: amount_share relative to its 5D mean (per concept)
    df = df.sort_values(["concept_code", "trade_date"]).copy()
    df["amt_share_ma5"] = (
        df.groupby("concept_code")["amount_share"]
        .rolling(5, min_periods=3)
        .mean()
        .reset_index(level=0, drop=True)
    )
    df["d_amt_share"] = (df["amount_share"] / df["amt_share_ma5"]) - 1.0

    latest = df[df["trade_date"] == latest_date].copy()
    if latest.empty:
        return pd.DataFrame(), [f"no concept data on latest_date={latest_date.date()}"]

    # cross-sectional percentile helper (0~1)
    def rank_pct(x: pd.Series) -> pd.Series:
        # pandas rank(pct=True) ignores NaN
        r = x.rank(pct=True)
        return r

    # Temperature (0-100): Temp = 0.5*amt_share + 0.2*Δamt_share + 0.3*ret_5d (all as cross-sectional percentile)
    p_amt = rank_pct(latest["amount_share"]).fillna(0.5)
    p_damt = rank_pct(latest["d_amt_share"]).fillna(0.5)
    p_ret5 = rank_pct(latest["ret_5d"]).fillna(0.5)
    latest["temperature"] = 100.0 * (0.5 * p_amt + 0.2 * p_damt + 0.3 * p_ret5)

    # Risk (0-100): Risk = 0.5*vol10 + 0.35*|dd10| + 0.15*ext10 (all as cross-sectional percentile)
    p_vol10 = rank_pct(latest["vol10"]).fillna(0.5)
    p_dd10 = rank_pct(latest["abs_dd10"]).fillna(0.5)
    p_ext10 = rank_pct(latest["ext10"]).fillna(0.5)
    latest["risk"] = 100.0 * (0.5 * p_vol10 + 0.35 * p_dd10 + 0.15 * p_ext10)

    # name map (concept_master)
    latest["name"] = latest["concept_code"].map(lambda c: name_map.get(str(c), ""))
    latest["name"] = latest["name"].replace("", np.nan)
    latest["name"] = latest["name"].fillna(latest["concept_code"].map(lambda c: f"概念{c}"))

    latest["amount_share_pct"] = latest["amount_share"] * 100.0

    show = latest[
        ["concept_code", "name", "temperature", "risk", "ret_1d", "ret_5d", "amount_share_pct"]
    ].copy()
    show["ret_1d"] = show["ret_1d"] * 100.0
    show["ret_5d"] = show["ret_5d"] * 100.0

    show = show.sort_values("temperature", ascending=False).head(int(top_k))
    return show, msgs


# ----------------------------
# App
# ----------------------------

# ----------------------------
# Intraday snapshot via AkShare (no DB writes)
# ----------------------------
def _now_with_tz_offset(hours_offset: int = 8) -> dt.datetime:
    """Return 'now' in a fixed UTC offset timezone (default: Beijing +8)."""
    try:
        tz = dt.timezone(dt.timedelta(hours=int(hours_offset)))
        return dt.datetime.now(tz=tz)
    except Exception:
        return dt.datetime.now()


def is_cn_trading_time(now: Optional[dt.datetime] = None, tz_offset_hours: int = 8) -> bool:
    """Rough CN A-share trading time check (09:30-11:30, 13:00-15:00), Beijing time by default."""
    if now is None:
        now = _now_with_tz_offset(tz_offset_hours)

    # must be a trading day
    if _today_trade_date_cn(now, tz_offset_hours) is None:
        return False

    t = now.time()
    # 09:30-11:30
    if (t >= dt.time(9, 30)) and (t <= dt.time(11, 30)):
        return True
    # 13:00-15:00
    if (t >= dt.time(13, 0)) and (t <= dt.time(15, 0)):
        return True
    return False


def try_import_akshare():
    try:
        import akshare as ak  # type: ignore
        return ak, None
    except Exception as e:
        return None, str(e)

def try_import_adata():
    """Import adata (optional)."""
    try:
        import adata  # type: ignore
        return adata, None
    except Exception as e:
        return None, str(e)


def _adata_get_market_module():
    adata, err = try_import_adata()
    if adata is None:
        raise RuntimeError(f"adata not available: {err}")
    # adata may expose stock as attribute or submodule
    stock_mod = getattr(adata, "stock", None)
    if stock_mod is None:
        try:
            from adata import stock as stock_mod  # type: ignore
        except Exception:
            stock_mod = None
    if stock_mod is None:
        raise RuntimeError("adata.stock not found")
    market_mod = getattr(stock_mod, "market", None)
    if market_mod is None:
        raise RuntimeError("adata.stock.market not found")
    return market_mod


# @st.cache_data(ttl=30, show_spinner=False)
def adata_fetch_concept_current_ths() -> pd.DataFrame:
    """Realtime THS concept index snapshot via adata: stock.market.get_market_concept_current_ths()."""
    m = _adata_get_market_module()
    fn = getattr(m, "get_market_concept_current_ths", None)
    if fn is None:
        raise RuntimeError("get_market_concept_current_ths not found in adata.stock.market")
    df = fn()
    if df is None:
        return pd.DataFrame()
    if isinstance(df, dict):
        df = pd.DataFrame(df)
    return pd.DataFrame(df)


@st.cache_data(ttl=30, show_spinner=False)
def adata_fetch_index_current() -> pd.DataFrame:
    """Realtime index snapshot via adata: stock.market.get_market_index_current()."""
    m = _adata_get_market_module()
    fn = getattr(m, "get_market_index_current", None)
    if fn is None:
        raise RuntimeError("get_market_index_current not found in adata.stock.market")
    df = fn()
    if df is None:
        return pd.DataFrame()
    if isinstance(df, dict):
        df = pd.DataFrame(df)
    return pd.DataFrame(df)


def normalize_rt_concept_df(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize adata realtime concept snapshot columns into: concept_code, change_pct, amount."""
    if df is None or df.empty:
        return pd.DataFrame(columns=["concept_code", "change_pct", "amount"])
    cols = list(df.columns)
    # code
    code_col = next((c for c in cols if str(c).lower() in ["index_code","concept_code","sec_code","code"]), None)
    if code_col is None:
        # try contains
        code_col = next((c for c in cols if "code" in str(c).lower()), None)
    # pct
    pct_col = next((c for c in cols if str(c).lower() in ["change_pct","pct_chg","pct","涨跌幅","涨跌幅%"]), None)
    if pct_col is None:
        pct_col = next((c for c in cols if "涨跌" in str(c) or "pct" in str(c).lower()), None)
    # amount
    amt_col = next((c for c in cols if str(c).lower() in ["amount","turnover","成交额","成交额(元)"]), None)
    if amt_col is None:
        amt_col = next((c for c in cols if "成交" in str(c) or "amount" in str(c).lower()), None)

    out = pd.DataFrame()
    out["concept_code"] = df[code_col].astype(str) if code_col else df.iloc[:,0].astype(str)
    out["concept_code"] = out["concept_code"].astype(str).str.replace(r"\D", "", regex=True).str.zfill(6)
    if pct_col:
        out["change_pct"] = df[pct_col].apply(safe_to_float)
    else:
        out["change_pct"] = np.nan
    # Heuristic: if values look like decimals (e.g., 0.0057), convert to percent points
    try:
        _m = pd.to_numeric(out["change_pct"], errors="coerce").abs().median()
        if np.isfinite(_m) and (_m < 0.2):
            out["change_pct"] = out["change_pct"] * 100.0
    except Exception:
        pass
    if amt_col:
        out["amount"] = df[amt_col].apply(safe_to_float)
    else:
        out["amount"] = np.nan
    out = out.dropna(subset=["concept_code"])
    return out


def normalize_rt_index_df(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize adata realtime index snapshot into: sec_code, change_pct, amount."""
    if df is None or df.empty:
        return pd.DataFrame(columns=["sec_code", "change_pct", "amount"])
    cols = list(df.columns)
    code_col = next((c for c in cols if str(c).lower() in ["index_code","sec_code","code","symbol"]), None)
    if code_col is None:
        code_col = next((c for c in cols if "code" in str(c).lower() or "symbol" in str(c).lower()), None)
    pct_col = next((c for c in cols if str(c).lower() in ["change_pct","pct_chg","pct","涨跌幅","涨跌幅%"]), None)
    if pct_col is None:
        pct_col = next((c for c in cols if "涨跌" in str(c) or "pct" in str(c).lower()), None)
    amt_col = next((c for c in cols if str(c).lower() in ["amount","turnover","成交额","成交额(元)"]), None)
    if amt_col is None:
        amt_col = next((c for c in cols if "成交" in str(c) or "amount" in str(c).lower()), None)

    out = pd.DataFrame()
    out["sec_code"] = df[code_col].astype(str) if code_col else df.iloc[:,0].astype(str)
    out["sec_code"] = out["sec_code"].apply(canonical_code)
    out["change_pct"] = df[pct_col].apply(safe_to_float) if pct_col else np.nan
    # Heuristic: if values look like decimals (e.g., 0.0057), convert to percent points
    try:
        _m = pd.to_numeric(out["change_pct"], errors="coerce").abs().median()
        if np.isfinite(_m) and (_m < 0.2):
            out["change_pct"] = out["change_pct"] * 100.0
    except Exception:
        pass
    out["amount"] = df[amt_col].apply(safe_to_float) if amt_col else np.nan
    out = out.dropna(subset=["sec_code"])
    return out


def _pick_df_col(df: pd.DataFrame, cands: List[str]) -> Optional[str]:
    for c in cands:
        if c in df.columns:
            return c
    # fallback: strip spaces / lower
    cols_map = {str(c).strip().lower(): c for c in df.columns}
    for c in cands:
        key = str(c).strip().lower()
        if key in cols_map:
            return cols_map[key]
    return None


@st.cache_data(ttl=60, show_spinner=False)
def ak_fetch_a_spot_em() -> pd.DataFrame:
    """All A-share realtime spot from Eastmoney (AkShare)."""
    ak, err = try_import_akshare()
    if ak is None:
        raise RuntimeError(f"akshare not available: {err}")
    return ak.stock_zh_a_spot_em()


@st.cache_data(ttl=60, show_spinner=False)
def ak_fetch_index_spot_em(symbol: str = "沪深重要指数") -> pd.DataFrame:
    """Index realtime spot from Eastmoney (AkShare)."""
    ak, err = try_import_akshare()
    if ak is None:
        raise RuntimeError(f"akshare not available: {err}")
    return ak.stock_zh_index_spot_em(symbol=symbol)


@st.cache_data(ttl=60, show_spinner=False)
def ak_fetch_a_spot_sina() -> pd.DataFrame:
    """All A-share realtime spot from Sina (AkShare)."""
    ak, err = try_import_akshare()
    if ak is None:
        raise RuntimeError(f"akshare not available: {err}")
    return ak.stock_zh_a_spot()


@st.cache_data(ttl=60, show_spinner=False)
def ak_fetch_ah_spot() -> pd.DataFrame:
    """A+H spot from Sina (AkShare), used as fallback."""
    ak, err = try_import_akshare()
    if ak is None:
        raise RuntimeError(f"akshare not available: {err}")
    return ak.stock_zh_ah_spot()


@st.cache_data(ttl=60, show_spinner=False)
def ak_fetch_index_spot_sina() -> pd.DataFrame:
    """Index realtime spot from Sina (AkShare)."""
    ak, err = try_import_akshare()
    if ak is None:
        raise RuntimeError(f"akshare not available: {err}")
    return ak.stock_zh_index_spot_sina()


@st.cache_data(ttl=60, show_spinner=False)
def ak_fetch_etf_spot_em() -> pd.DataFrame:
    """ETF realtime spot list from Eastmoney (AkShare). Used as index proxy fallback."""
    ak, err = try_import_akshare()
    if ak is None:
        raise RuntimeError(f"akshare not available: {err}")
    return ak.fund_etf_spot_em()


def em_fetch_index_pct(code6: str) -> float:
    """Fetch realtime % change for an index via Eastmoney push2 API (no AkShare dependency)."""
    code6 = canonical_code(code6)
    secid_map = {
        # Common indices used in this app
        "000300": "1.000300",  # CSI 300
        "000852": "1.000852",  # CSI 1000
    }
    secid = secid_map.get(code6)
    if not secid:
        # best-effort: assume SH=1 for indices
        secid = f"1.{code6}"
    url = "https://push2.eastmoney.com/api/qt/stock/get"
    params = {"secid": secid, "fields": "f170,f43,f60"}  # pct, last, prev_close
    r = requests.get(url, params=params, timeout=6)
    r.raise_for_status()
    j = r.json()
    data = (j or {}).get("data") or {}
    pct = safe_to_float(data.get("f170"))
    if np.isfinite(pct):
        return float(pct)
    last = safe_to_float(data.get("f43"))
    prev = safe_to_float(data.get("f60"))
    if np.isfinite(last) and np.isfinite(prev) and prev > 0:
        return float((last / prev - 1.0) * 100.0)
    return np.nan

def normalize_a_spot_df(df: pd.DataFrame) -> Tuple[pd.DataFrame, str]:
    """Normalize A-share spot df to sec_code(6d), change_pct(%), amount(yuan best-effort)."""
    if df is None or df.empty:
        return pd.DataFrame(columns=["sec_code", "change_pct", "amount"]), "empty"
    ccol = _pick_df_col(df, ["代码", "code", "symbol", "证券代码", "股票代码"])
    pcol = _pick_df_col(df, ["涨跌幅", "涨跌幅%", "涨跌幅(%)", "change_percent", "pct_chg", "pct", "PCT_CHG", "changepercent"])
    acol = _pick_df_col(df, ["成交额", "amount", "成交额(元)", "成交额(万元)", "成交额(万)", "turnover", "成交金额"])
    note = f"code={ccol},pct={pcol},amt={acol}"
    if ccol is None or pcol is None:
        return pd.DataFrame(columns=["sec_code", "change_pct", "amount"]), "missing_code_or_pct|" + note
    out = pd.DataFrame()
    out["sec_code"] = df[ccol].astype(str).str.strip().apply(canonical_code)
    out["change_pct"] = df[pcol].apply(safe_to_float)
    if acol is not None:
        tmp = pd.to_numeric(df[acol], errors="coerce")
        med = float(tmp.dropna().median()) if tmp.dropna().size else float("nan")
        if np.isfinite(med) and med < 1e8:
            tmp = tmp * 1e4
            note += "|amt_unit=1e4"
        out["amount"] = tmp
    else:
        out["amount"] = np.nan
    out = out[out["sec_code"].str.match(r"^(0|3|6)\d{5}$", na=False)].copy()
    out = out.dropna(subset=["sec_code", "change_pct"])
    return out, note


def normalize_index_spot_df(df: pd.DataFrame) -> Tuple[pd.DataFrame, str]:
    """Normalize index spot df to sec_code(6d), change_pct(%)."""
    if df is None or df.empty:
        return pd.DataFrame(columns=["sec_code", "change_pct"]), "empty"
    ccol = _pick_df_col(df, ["代码", "code", "symbol", "证券代码", "index_code"])
    pcol = _pick_df_col(df, ["涨跌幅", "涨跌幅%", "涨跌幅(%)", "change_percent", "pct_chg", "pct", "PCT_CHG"])
    note = f"code={ccol},pct={pcol}"
    if ccol is None or pcol is None:
        return pd.DataFrame(columns=["sec_code", "change_pct"]), "missing|" + note
    out = pd.DataFrame()
    out["sec_code"] = df[ccol].astype(str).str.strip().apply(canonical_code)
    out["change_pct"] = df[pcol].apply(safe_to_float)
    out = out.dropna(subset=["sec_code", "change_pct"])
    return out, note



# ----------------------------
# Close sync: write today's close snapshot to DB (stocks / indexes / THS concepts)
# ----------------------------

CN_CLOSE_DATA_READY_TIME = dt.time(17, 30)

def _is_after_close_cn(now_cn: Optional[dt.datetime] = None, tz_offset_hours: int = 8) -> bool:
    """After A-share close AND most public data sources are usually ready (>=17:30 Beijing time)."""
    if now_cn is None:
        now_cn = _now_with_tz_offset(tz_offset_hours)
    return now_cn.time() >= CN_CLOSE_DATA_READY_TIME


def _is_snapshot_prefer_window_cn(now_cn: Optional[dt.datetime] = None, tz_offset_hours: int = 8) -> bool:
    """Prefer intraday snapshot during 09:30-17:30 Beijing time on trading days."""
    if now_cn is None:
        now_cn = _now_with_tz_offset(tz_offset_hours)

    # Only prefer snapshot on a valid trading day
    td = _today_trade_date_cn(now_cn, tz_offset_hours)
    if td is None:
        return False

    t = now_cn.time()
    return (t >= dt.time(9, 30)) and (t < CN_CLOSE_DATA_READY_TIME)


def _today_trade_date_cn(now_cn: Optional[dt.datetime] = None, tz_offset_hours: int = 8) -> Optional[pd.Timestamp]:
    """Return today's trading date (Timestamp) if today is a CN trading day, else None."""
    if now_cn is None:
        now_cn = _now_with_tz_offset(tz_offset_hours)
    today = pd.Timestamp(now_cn.date())
    latest_trade = get_latest_cn_trade_date(today)
    if latest_trade is None:
        return None
    return today if pd.Timestamp(latest_trade.date()) == today else None


def last_completed_trade_date_cn(now_cn: Optional[dt.datetime] = None, tz_offset_hours: int = 8) -> pd.Timestamp:
    """Most recent *completed* CN trading date (close reference).

    Rules:
    - If today is a trading day and local time >= 17:30, use today.
    - Otherwise (including 00:00-09:30 and 15:00-17:30), use the previous trading day.
    - If today is not a trading day, use the latest trading day <= today.
    """
    if now_cn is None:
        now_cn = _now_with_tz_offset(tz_offset_hours)

    today = pd.Timestamp(now_cn.date())
    try:
        if _today_trade_date_cn(now_cn, tz_offset_hours) is not None:
            if now_cn.time() >= CN_CLOSE_DATA_READY_TIME:
                return today
            prev = get_cn_last_trade_date(today - pd.Timedelta(days=1))
            return prev if prev is not None else today
        last_td = get_cn_last_trade_date(today)
        return last_td if last_td is not None else today
    except Exception:
        # very defensive fallback
        return today

def _scale_amount_to_yuan(series: pd.Series) -> pd.Series:
    """Best-effort: infer成交额单位并统一到 元."""
    s = pd.to_numeric(series, errors="coerce")
    med = float(s.dropna().median()) if s.dropna().size else float("nan")
    if not np.isfinite(med):
        return s
    # typical A-share amount in yuan: 1e8~1e11; if too small, likely in 万元
    if med < 1e8:
        return s * 1e4
    return s


def normalize_a_spot_df_full(df: pd.DataFrame) -> Tuple[pd.DataFrame, str]:
    """Normalize A-share spot into daily-like fields: sec_code, close, pre_close, open, high, low, volume, amount, change_pct."""
    if df is None or df.empty:
        return pd.DataFrame(columns=["sec_code","close","pre_close","open","high","low","volume","amount","change_pct"]), "empty"
    ccol = _pick_df_col(df, ["代码","证券代码","股票代码","code","symbol"])
    price_col = _pick_df_col(df, ["最新价","最新","现价","price","last","close","收盘"])
    pct_col = _pick_df_col(df, ["涨跌幅","涨跌幅%","涨跌幅(%)","change_percent","pct_chg","pct","PCT_CHG","changepercent"])
    amt_col = _pick_df_col(df, ["成交额","成交额(元)","成交额(万元)","成交额(万)","amount","turnover","成交金额"])
    vol_col = _pick_df_col(df, ["成交量","volume","vol","成交量(手)"])
    open_col = _pick_df_col(df, ["今开","开盘","open"])
    high_col = _pick_df_col(df, ["最高","high"])
    low_col = _pick_df_col(df, ["最低","low"])
    pre_col = _pick_df_col(df, ["昨收","昨收价","pre_close","preClose","previous_close"])

    note = f"code={ccol},price={price_col},pct={pct_col},amt={amt_col},vol={vol_col}"
    if ccol is None or price_col is None:
        return pd.DataFrame(columns=["sec_code","close","pre_close","open","high","low","volume","amount","change_pct"]), "missing_code_or_price|" + note

    out = pd.DataFrame()
    out["sec_code"] = df[ccol].astype(str).str.strip().apply(canonical_code)
    out["close"] = df[price_col].apply(safe_to_float)
    if pre_col is not None:
        out["pre_close"] = df[pre_col].apply(safe_to_float)
    else:
        out["pre_close"] = np.nan
    if open_col is not None:
        out["open"] = df[open_col].apply(safe_to_float)
    else:
        out["open"] = np.nan
    if high_col is not None:
        out["high"] = df[high_col].apply(safe_to_float)
    else:
        out["high"] = np.nan
    if low_col is not None:
        out["low"] = df[low_col].apply(safe_to_float)
    else:
        out["low"] = np.nan
    if vol_col is not None:
        out["volume"] = pd.to_numeric(df[vol_col], errors="coerce")
    else:
        out["volume"] = np.nan
    if amt_col is not None:
        out["amount"] = _scale_amount_to_yuan(df[amt_col])
    else:
        out["amount"] = np.nan

    # pct: prefer source; fallback to (close/pre_close-1)*100
    if pct_col is not None:
        out["change_pct"] = df[pct_col].apply(safe_to_float)
    else:
        out["change_pct"] = np.nan
    mask_need = (~np.isfinite(out["change_pct"])) & np.isfinite(out["pre_close"]) & (out["pre_close"] > 0) & np.isfinite(out["close"])
    out.loc[mask_need, "change_pct"] = (out.loc[mask_need, "close"] / out.loc[mask_need, "pre_close"] - 1.0) * 100.0

    out = out.dropna(subset=["sec_code", "close"])
    out = out[out["sec_code"].astype(str).str.len() == 6]
    return out, note


def normalize_index_spot_df_full(df: pd.DataFrame) -> Tuple[pd.DataFrame, str]:
    """Normalize index spot into daily-like fields."""
    if df is None or df.empty:
        return pd.DataFrame(columns=["sec_code","close","pre_close","amount","change_pct"]), "empty"
    ccol = _pick_df_col(df, ["代码","证券代码","index_code","code","symbol"])
    price_col = _pick_df_col(df, ["最新价","现价","最新","price","last","close","收盘"])
    pct_col = _pick_df_col(df, ["涨跌幅","涨跌幅%","涨跌幅(%)","change_percent","pct_chg","pct","PCT_CHG"])
    amt_col = _pick_df_col(df, ["成交额","成交额(元)","amount","turnover","成交金额"])
    pre_col = _pick_df_col(df, ["昨收","昨收价","pre_close","preClose","previous_close"])
    note = f"code={ccol},price={price_col},pct={pct_col},amt={amt_col}"
    if ccol is None or price_col is None:
        return pd.DataFrame(columns=["sec_code","close","pre_close","amount","change_pct"]), "missing|" + note

    out = pd.DataFrame()
    out["sec_code"] = df[ccol].astype(str).str.strip().apply(canonical_code)
    out["close"] = df[price_col].apply(safe_to_float)
    out["pre_close"] = df[pre_col].apply(safe_to_float) if pre_col is not None else np.nan
    out["amount"] = _scale_amount_to_yuan(df[amt_col]) if amt_col is not None else np.nan
    out["change_pct"] = df[pct_col].apply(safe_to_float) if pct_col is not None else np.nan
    mask_need = (~np.isfinite(out["change_pct"])) & np.isfinite(out["pre_close"]) & (out["pre_close"] > 0) & np.isfinite(out["close"])
    out.loc[mask_need, "change_pct"] = (out.loc[mask_need, "close"] / out.loc[mask_need, "pre_close"] - 1.0) * 100.0
    out = out.dropna(subset=["sec_code","close"])
    out = out[out["sec_code"].astype(str).str.len() == 6]
    return out, note


def _write_kline_daily_snapshot(stock_db: str, sec_type: str, trade_date: str, snap_df: pd.DataFrame) -> Tuple[int, List[str]]:
    """Write snapshot rows into kline_daily for a given sec_type on trade_date. Returns (n_rows, msgs)."""
    msgs: List[str] = []
    if snap_df is None or snap_df.empty:
        return 0, [f"{sec_type}: snapshot empty"]
    con = connect(stock_db)
    try:
        if not table_exists(con, "kline_daily"):
            return 0, [f"{sec_type}: kline_daily not found in {stock_db}"]
        schema = detect_kline_schema_stock(con)
        if any(schema.get(k) is None for k in ["sec_type","code","date","close","pct","amount"]):
            return 0, [f"{sec_type}: kline_daily schema missing required columns: {schema}"]

        cols = get_columns(con, "kline_daily")
        # optional columns
        open_col = pick_col(cols, ["open","OPEN","o"])
        high_col = pick_col(cols, ["high","HIGH","h"])
        low_col  = pick_col(cols, ["low","LOW","l"])
        vol_col  = pick_col(cols, ["volume","vol","VOLUME","VOL"])
        pre_col  = pick_col(cols, ["pre_close","preClose","PRE_CLOSE","yesterday_close","昨收"])

        # delete old rows for this day/sec_type
        date_col = schema["date"]
        sec_type_col = schema["sec_type"]
        con.execute(
            f"DELETE FROM kline_daily WHERE {sec_type_col}=? AND substr(replace({date_col},'/','-'),1,10)=?",
            (sec_type, trade_date),
        )
        con.commit()

        # build write frame
        w = pd.DataFrame()
        w["sec_type"] = sec_type
        w["sec_code"] = snap_df["sec_code"].astype(str).str.zfill(6)
        w["trade_date"] = trade_date
        w["close"] = pd.to_numeric(snap_df["close"], errors="coerce")
        w["change_pct"] = pd.to_numeric(snap_df.get("change_pct", np.nan), errors="coerce")
        w["amount"] = pd.to_numeric(snap_df.get("amount", np.nan), errors="coerce")

        if open_col and ("open" in snap_df.columns):
            w["open"] = pd.to_numeric(snap_df["open"], errors="coerce")
        if high_col and ("high" in snap_df.columns):
            w["high"] = pd.to_numeric(snap_df["high"], errors="coerce")
        if low_col and ("low" in snap_df.columns):
            w["low"] = pd.to_numeric(snap_df["low"], errors="coerce")
        if vol_col and ("volume" in snap_df.columns):
            w["volume"] = pd.to_numeric(snap_df["volume"], errors="coerce")
        if pre_col and ("pre_close" in snap_df.columns):
            w["pre_close"] = pd.to_numeric(snap_df["pre_close"], errors="coerce")

        mapping = {
            "sec_type": schema["sec_type"],
            "sec_code": schema["code"],
            "trade_date": schema["date"],
            "close": schema["close"],
            "change_pct": schema["pct"],
            "amount": schema["amount"],
        }
        if open_col and "open" in w.columns:
            mapping["open"] = open_col
        if high_col and "high" in w.columns:
            mapping["high"] = high_col
        if low_col and "low" in w.columns:
            mapping["low"] = low_col
        if vol_col and "volume" in w.columns:
            mapping["volume"] = vol_col
        if pre_col and "pre_close" in w.columns:
            mapping["pre_close"] = pre_col

        w2 = w[list(mapping.keys())].rename(columns=mapping)
        w2 = w2.dropna(subset=[schema["code"], schema["close"]])

        if w2.empty:
            return 0, [f"{sec_type}: no valid rows after normalization"]

        write_cols = list(w2.columns)
        ph = ",".join(["?"] * len(write_cols))
        sql = f"INSERT INTO kline_daily ({','.join(write_cols)}) VALUES ({ph})"
        con.executemany(sql, list(w2.itertuples(index=False, name=None)))
        con.commit()
        n = int(len(w2))
        msgs.append(f"{sec_type}: wrote {n} rows into kline_daily for {trade_date}")
        return n, msgs
    except Exception as e:
        return 0, [f"{sec_type}: write kline_daily failed: {e}"]
    finally:
        try:
            con.close()
        except Exception:
            pass


def _write_concept_kline_snapshot(concept_db: str, trade_date: str, rt_concept_df: pd.DataFrame) -> Tuple[int, List[str]]:
    """Write THS concept snapshot into concept_kline_ths on trade_date. close is inferred from prev close + pct."""
    msgs: List[str] = []
    if not concept_db or (not os.path.exists(concept_db)):
        return 0, ["concept: db not found"]
    if rt_concept_df is None or rt_concept_df.empty:
        return 0, ["concept: realtime snapshot empty"]

    con = connect(concept_db)
    try:
        table = "concept_kline_ths" if table_exists(con, "concept_kline_ths") else None
        if table is None:
            # try legacy
            for t in ["concept_kline", "ths_concept_kline", "kline_concept"]:
                if table_exists(con, t):
                    table = t
                    break
        if table is None:
            return 0, [f"concept: no concept kline table found in {concept_db}"]

        schema = detect_concept_kline_schema(con, table)
        if schema["code"] is None or schema["date"] is None or schema["pct"] is None or schema["amount"] is None:
            return 0, [f"concept: schema missing columns: {schema}"]

        # prev day close for each concept (for close inference)
        prev_date = sql_max_date(con, f"SELECT MAX({schema['date']}) FROM {table}")
        prev_date = normalize_date(prev_date) if prev_date else None

        prev_map = None
        if prev_date:
            try:
                df_prev = df_from_sql(
                    con,
                    f"SELECT {schema['code']} as concept_code, {schema['close']} as prev_close FROM {table} WHERE substr(replace({schema['date']},'/','-'),1,10)=?",
                    (prev_date,),
                )
                if df_prev is not None and not df_prev.empty:
                    df_prev["concept_code"] = df_prev["concept_code"].astype(str).str.strip().apply(canonical_code)
                    df_prev["prev_close"] = df_prev["prev_close"].apply(safe_to_float)
                    prev_map = df_prev.set_index("concept_code")["prev_close"].to_dict()
            except Exception:
                prev_map = None

        # normalize realtime concept snapshot
        df_rt = normalize_rt_concept_df(rt_concept_df)
        if df_rt.empty:
            return 0, ["concept: realtime normalization empty"]

        df_rt = df_rt.copy()
        df_rt["concept_code"] = df_rt["concept_code"].astype(str).str.strip().apply(canonical_code)
        df_rt["change_pct"] = df_rt["change_pct"].apply(safe_to_float)
        df_rt["amount"] = _scale_amount_to_yuan(df_rt["amount"])

        # infer close/pre_close
        if prev_map:
            df_rt["pre_close"] = df_rt["concept_code"].map(prev_map)
        else:
            df_rt["pre_close"] = np.nan
        df_rt["close"] = np.nan
        mask = np.isfinite(df_rt["pre_close"]) & (df_rt["pre_close"] > 0) & np.isfinite(df_rt["change_pct"])
        df_rt.loc[mask, "close"] = df_rt.loc[mask, "pre_close"] * (1.0 + df_rt.loc[mask, "change_pct"] / 100.0)

        # write
        date_col = schema["date"]
        con.execute(f"DELETE FROM {table} WHERE substr(replace({date_col},'/','-'),1,10)=?", (trade_date,))
        con.commit()

        w = pd.DataFrame()
        w["concept_code"] = df_rt["concept_code"]
        w["trade_date"] = trade_date
        if schema.get("close"):
            w["close"] = df_rt["close"]
        if schema.get("pre_close"):
            w["pre_close"] = df_rt["pre_close"]
        w["change_pct"] = df_rt["change_pct"]
        w["amount"] = df_rt["amount"]
        if schema.get("volume"):
            w["volume"] = np.nan

        mapping = {
            "concept_code": schema["code"],
            "trade_date": schema["date"],
            "change_pct": schema["pct"],
            "amount": schema["amount"],
        }
        if schema.get("close"):
            mapping["close"] = schema["close"]
        if schema.get("pre_close"):
            mapping["pre_close"] = schema["pre_close"]
        if schema.get("volume") and "volume" in w.columns:
            mapping["volume"] = schema["volume"]

        w2 = w[list(mapping.keys())].rename(columns=mapping)
        # require concept_code and pct
        w2 = w2.dropna(subset=[schema["code"], schema["pct"]])
        if w2.empty:
            return 0, ["concept: no valid rows to write"]

        write_cols = list(w2.columns)
        ph = ",".join(["?"] * len(write_cols))
        sql = f"INSERT INTO {table} ({','.join(write_cols)}) VALUES ({ph})"
        con.executemany(sql, list(w2.itertuples(index=False, name=None)))
        con.commit()
        n = int(len(w2))
        msgs.append(f"concept: wrote {n} rows into {table} for {trade_date} (close inferred from prev_close+pct)")
        return n, msgs
    except Exception as e:
        return 0, [f"concept: write failed: {e}"]
    finally:
        try:
            con.close()
        except Exception:
            pass



def sync_today_close_to_db_if_available(stock_db: str, concept_db: Optional[str], tz_offset_hours: int = 8, force: bool = False) -> Tuple[bool, List[str]]:
    """If after close and today is trading day, try to write today's close snapshot into DB.

    - stock_db: write ALL A-share + important indexes into kline_daily
    - concept_db: write THS concept snapshot into concept_kline_ths (best-effort; won't block success)
    Returns (success, msgs).
    """
    msgs: List[str] = []
    now_cn = _now_with_tz_offset(int(tz_offset_hours))
    today_trade = _today_trade_date_cn(now_cn, int(tz_offset_hours))
    if today_trade is None:
        return False, ["today is not a CN trading day (calendar)"]
    trade_date = today_trade.strftime("%Y-%m-%d")

    if (not _is_after_close_cn(now_cn, int(tz_offset_hours))) and (not force):
        return False, [f"not after close yet (now={now_cn.strftime('%H:%M:%S')}, need >=17:30)"]

    # detect whether today's data already exists (stock/index separately)
    has_stock_today = False
    has_index_today = False
    try:
        con = connect(stock_db)
        if table_exists(con, "kline_daily"):
            schema = detect_kline_schema_stock(con)
            if all(schema.get(k) is not None for k in ["sec_type","date"]):
                sec_type_col = schema["sec_type"]
                date_col = schema["date"]
                has_stock_today = con.execute(
                    f"SELECT 1 FROM kline_daily WHERE {sec_type_col}='stock' AND substr(replace({date_col},'/','-'),1,10)=? LIMIT 1",
                    (trade_date,),
                ).fetchone() is not None
                has_index_today = con.execute(
                    f"SELECT 1 FROM kline_daily WHERE {sec_type_col}='index' AND substr(replace({date_col},'/','-'),1,10)=? LIMIT 1",
                    (trade_date,),
                ).fetchone() is not None
        con.close()
    except Exception:
        pass

    # 1) stocks: all A spot (em -> sina fallback)
    stock_rows = 0
    if (not has_stock_today) or force:
        a_spot = None
        a_note = ""
        try:
            a_spot = ak_fetch_a_spot_em()
            a_note = "ak.stock_zh_a_spot_em"
        except Exception as e1:
            try:
                a_spot = ak_fetch_a_spot_sina()
                a_note = "ak.stock_zh_a_spot (sina)"
            except Exception as e2:
                msgs.append(f"stock spot fetch failed: em={e1}; sina={e2}")
                a_spot = None

        if a_spot is not None:
            a_full, note = normalize_a_spot_df_full(a_spot)
            msgs.append(f"stock spot source={a_note}, {note}, n={len(a_full)}")
            # require enough coverage
            if len(a_full) >= 2000:
                stock_rows, m = _write_kline_daily_snapshot(stock_db, "stock", trade_date, a_full)
                msgs.extend(m)
            else:
                msgs.append("stock: coverage too low, will not write to DB")
        else:
            msgs.append("stock: no spot dataframe, skip write")
    else:
        msgs.append(f"stock: DB already has {trade_date} (skip)")

    # 2) indexes: important indices snapshot (eastmoney)
    idx_rows = 0
    if (not has_index_today) or force:
        try:
            idx_spot = ak_fetch_index_spot_em("沪深重要指数")
            idx_full, note = normalize_index_spot_df_full(idx_spot)
            msgs.append(f"index spot source=ak.stock_zh_index_spot_em, {note}, n={len(idx_full)}")
            if len(idx_full) > 0:
                idx_rows, m = _write_kline_daily_snapshot(stock_db, "index", trade_date, idx_full)
                msgs.extend(m)
            else:
                msgs.append("index: snapshot empty, skip write")
        except Exception as e:
            msgs.append(f"index spot fetch/write failed: {e}")
    else:
        msgs.append(f"index: DB already has {trade_date} (skip)")

    # 3) concepts: ths concepts via adata (best-effort; do not block success)
    concept_rows = 0
    if concept_db:
        try:
            rt = adata_fetch_concept_current_ths()
            concept_rows, m = _write_concept_kline_snapshot(concept_db, trade_date, rt)
            msgs.extend(m)
        except Exception as e:
            msgs.append(f"concept snapshot failed: {e}")

    # decide success: as long as stock+index are available (either already existed or written)
    ok_stock = has_stock_today or (stock_rows >= 2000)
    ok_index = has_index_today or (idx_rows > 0)
    success = bool(ok_stock and ok_index)

    if concept_db and concept_rows < 100:
        msgs.append(f"concept: not updated or too few rows (n={concept_rows}), panel will fallback to adata realtime for concepts if needed")

    if not success:
        msgs.append(f"sync result: has_stock_today={has_stock_today}, has_index_today={has_index_today}, stock_rows={stock_rows}, idx_rows={idx_rows}, concept_rows={concept_rows}")
    return success, msgs



def _st_rerun_safe():
    try:
        st.rerun()
    except Exception:
        try:
            st.experimental_rerun()
        except Exception:
            pass



def infer_rel_from_pct_diff(diff: float) -> float:
    """
    Infer (zz1000 - hs300) relative return in *fraction* form from a diff that may be:
      - percent-point diff (e.g., 0.57 meaning 0.57%)
      - fraction diff (e.g., 0.0057 meaning 0.57%)
      - occasionally 57 meaning 0.57% (bad scaling)
    Returns a fraction (0.0057).
    """
    diff = safe_to_float(diff)
    if not np.isfinite(diff):
        return np.nan

    # If diff is huge, likely scaled by 100 (or more)
    if abs(diff) > 20:
        # 57 -> 0.57% -> 0.0057
        return float(diff) / 10000.0

    cand_pp = float(diff) / 100.0   # treat as percent-points
    cand_frac = float(diff)         # treat as fraction already

    def plausible(x: float) -> bool:
        return np.isfinite(x) and (abs(x) <= 0.20)  # relative between indices unlikely >20%

    # Prefer the candidate that is plausible and not "too tiny"
    tiny = 5e-4  # 0.05%
    pp_ok = plausible(cand_pp)
    fr_ok = plausible(cand_frac)

    if pp_ok and (not fr_ok):
        return cand_pp
    if fr_ok and (not pp_ok):
        return cand_frac

    # both plausible or both implausible:
    # if one is tiny and the other isn't, prefer the non-tiny
    if pp_ok and fr_ok:
        if abs(cand_pp) < tiny and abs(cand_frac) >= tiny:
            return cand_frac
        if abs(cand_frac) < tiny and abs(cand_pp) >= tiny:
            return cand_pp
        # otherwise choose the smaller magnitude (conservative)
        return cand_pp if abs(cand_pp) <= abs(cand_frac) else cand_frac

    # both implausible -> fall back to percent-points conversion
    return cand_pp


def ak_fetch_a_spot_any() -> Tuple[pd.DataFrame, str]:
    """Try multiple AkShare endpoints for all A-share realtime quotes."""
    last = None
    for fn, name in [
        (ak_fetch_a_spot_em, "stock_zh_a_spot_em"),
        (ak_fetch_a_spot_sina, "stock_zh_a_spot"),
        (ak_fetch_ah_spot, "stock_zh_ah_spot"),
    ]:
        try:
            df = fn()
            norm, note = normalize_a_spot_df(df)
            if (norm is not None) and (not norm.empty):
                # basic sanity: need enough rows and pct not all NaN
                nn = int(len(norm))
                valid_pct = int(pd.to_numeric(norm['change_pct'], errors='coerce').notna().sum()) if 'change_pct' in norm.columns else 0
                if nn >= 1000 and valid_pct >= max(200, nn//5):
                    return norm, f"{name}|{note}"
                last = f"{name} invalid: n={nn}, valid_pct={valid_pct} | {note}"
            last = f"{name} empty/missing: {note}"
        except Exception as e:
            last = f"{name} failed: {e}"
            continue
    raise RuntimeError(last or "all A-share spot endpoints failed")


def ak_fetch_index_spot_any() -> Tuple[pd.DataFrame, str]:
    """Try multiple endpoints for index realtime quotes."""
    last = None
    try:
        idx_all = []
        for sym in ["沪深重要指数", "中证系列指数", "深证系列指数", "上证系列指数"]:
            try:
                idx_all.append(ak_fetch_index_spot_em(symbol=sym))
            except Exception:
                pass
        if idx_all:
            df = pd.concat(idx_all, ignore_index=True).drop_duplicates()
            norm, note = normalize_index_spot_df(df)
            if not norm.empty:
                return norm, f"stock_zh_index_spot_em|{note}"
            last = f"index_spot_em empty/missing: {note}"
    except Exception as e:
        last = f"index_spot_em failed: {e}"

    try:
        df = ak_fetch_index_spot_sina()
        norm, note = normalize_index_spot_df(df)
        if not norm.empty:
            return norm, f"stock_zh_index_spot_sina|{note}"
        last = f"index_spot_sina empty/missing: {note}"
    except Exception as e:
        last = f"index_spot_sina failed: {e}"

    raise RuntimeError(last or "all index spot endpoints failed")


def _etf_proxy_pct(code: str, etf_spot: pd.DataFrame) -> float:
    """Get ETF pct change (percent) from fund_etf_spot_em()."""
    if etf_spot is None or etf_spot.empty:
        return np.nan
    ccol = _pick_df_col(etf_spot, ["代码", "code", "基金代码", "symbol"])
    pcol = _pick_df_col(etf_spot, ["涨跌幅", "涨跌幅%", "涨跌幅(%)", "change_percent", "pct_chg", "pct"])
    if ccol is None or pcol is None:
        return np.nan
    df = etf_spot[[ccol, pcol]].copy()
    df.columns = ["code", "pct"]
    df["code"] = df["code"].astype(str).str.strip()
    hit = df.loc[df["code"] == str(code), "pct"]
    if hit.empty:
        return np.nan
    return float(safe_to_float(hit.iloc[0]))

@st.cache_data(ttl=60, show_spinner=False)

@st.cache_data(ttl=60, show_spinner=False)
def ak_fetch_index_min_em(code: str) -> pd.DataFrame:
    """Index minute data from Eastmoney (AkShare), used as fallback when spot list misses codes."""
    ak, err = try_import_akshare()
    if ak is None:
        raise RuntimeError(f"akshare not available: {err}")
    # Different AkShare versions may have different signatures; try robust calls.
    try:
        return ak.index_zh_a_hist_min_em(symbol=str(code))
    except TypeError:
        return ak.index_zh_a_hist_min_em(symbol=str(code), period="1")


def ak_fetch_500etf_qvix_min() -> pd.DataFrame:
    """500ETF QVIX intraday series (AkShare)."""
    ak, err = try_import_akshare()
    if ak is None:
        raise RuntimeError(f"akshare not available: {err}")
    # documented function name in AkShare: index_option_500etf_min_qvix
    return ak.index_option_500etf_min_qvix()


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def ak_fetch_500etf_qvix_daily() -> pd.DataFrame:
    """500ETF QVIX daily series (AkShare)."""
    ak, err = try_import_akshare()
    if ak is None:
        raise RuntimeError(f"akshare not available: {err}")
    # documented function name in AkShare: index_option_500etf_qvix
    return ak.index_option_500etf_qvix()


def _qvix_value_from_df(df: pd.DataFrame) -> float:
    if df is None or df.empty:
        return np.nan
    vcol = _pick_df_col(df, ["QVIX", "qvix", "value", "close", "收盘", "最新价"])
    if vcol is None:
        # try numeric last column
        for c in df.columns[::-1]:
            if pd.api.types.is_numeric_dtype(df[c]):
                vcol = c
                break
    if vcol is None:
        return np.nan
    try:
        return float(pd.to_numeric(df[vcol], errors="coerce").dropna().iloc[-1])
    except Exception:
        return np.nan


def _qvix_pctile(qvix_now: float, hist: pd.Series, window: int = 252) -> float:
    if not np.isfinite(qvix_now):
        return np.nan
    s = pd.to_numeric(hist, errors="coerce").dropna().astype(float)
    if s.empty:
        return np.nan
    s = s.tail(int(window)) if len(s) > window else s
    if s.empty:
        return np.nan
    return float((s <= qvix_now).sum() / len(s))


def compute_intraday_snapshot_row(
    daily: pd.DataFrame,
    weights: Dict[str, float],
    hot_top_n: int = 100,
    tz_offset_hours: int = 8,
    index_prev_close: Optional[Dict[str, float]] = None,
    allow_override_db_today: bool = False,
) -> Tuple[Optional[pd.Series], Dict[str, float], List[str]]:
    """
    Build a provisional 'today' row from AkShare realtime spot data, without writing DB.

    Rules (per your spec):
      - For indicators that can be computed from realtime quotes: compute intraday values.
      - For ETF share pct & turnover_rel_ma10 (post-close): use yesterday (latest DB day) values.
      - QVIX and QVIX percentile: display only, do NOT affect sentiment score.

    Returns:
      today_row (Series with same columns as daily), qvix_info dict, msgs list
    """
    msgs: List[str] = []
    qvix_info: Dict[str, float] = {"qvix": np.nan, "qvix_pctile": np.nan}

    if daily is None or daily.empty:
        return None, qvix_info, ["daily empty: cannot build intraday snapshot"]

    d = daily.copy().sort_values("trade_date")
    d["trade_date"] = d["trade_date"].apply(normalize_date)

    latest_dt = d["trade_date"].max()
    latest_row = d[d["trade_date"] == latest_dt].iloc[-1]

    today_ts = pd.Timestamp(_now_with_tz_offset(tz_offset_hours).date())
    has_today_in_db = bool((d["trade_date"] == today_ts).any())
    if has_today_in_db and (not allow_override_db_today):
        # DB already has today's row; by default we do not override it.
        return None, qvix_info, msgs
    # For post-close factors (ETF shares / turnover_rel_ma10), always reference the latest *completed* DB day.
    # If DB already has today's partial row, use the previous trading day as reference.
    ref_dt = latest_dt
    if has_today_in_db:
        uniq = sorted(d["trade_date"].unique())
        if len(uniq) >= 2:
            ref_dt = uniq[-2]

    # ---- QVIX (display only) ----
    try:
        qmin = ak_fetch_500etf_qvix_min()
        qvix_now = _qvix_value_from_df(qmin)
        if not np.isfinite(qvix_now):
            qdaily = ak_fetch_500etf_qvix_daily()
            qvix_now = _qvix_value_from_df(qdaily)
        qvix_info["qvix"] = qvix_now
        if not np.isfinite(qvix_now):
            msgs.append("QVIX 返回为空（可能数据源不可达/未开盘/网络受限），该格将显示为 —")
        try:
            qdaily = ak_fetch_500etf_qvix_daily()
            dcol = _pick_df_col(qdaily, ["QVIX", "qvix", "value", "close", "收盘", "最新价"])
            if dcol is not None:
                qvix_info["qvix_pctile"] = _qvix_pctile(qvix_now, qdaily[dcol])
        except Exception:
            pass
    except Exception as e:
        msgs.append(f"QVIX 获取失败：{e}")
    # ---- A-share spot (realtime) ----
    try:
        s, spot_src = ak_fetch_a_spot_any()
        msgs.append(f"A股实时快照来源: {spot_src} | n={len(s)}")
    except Exception as e:
        return None, qvix_info, msgs + [f"AkShare 实时A股行情获取失败：{e}"]

    # filter to沪深A（避免京市/ETF/其他）
    s0 = s.copy()
    try:
        s = s[s["sec_code"].astype(str).apply(lambda x: A_SHARE_PREFIX_RE.match(str(x)) is not None)].copy()
    except Exception:
        # if regex unexpected, do not filter
        s = s0.copy()

    # If filtering accidentally removed everything but raw spot had thousands of rows, keep unfiltered
    if s.empty and (s0 is not None) and (not s0.empty) and len(s0) >= 1000:
        msgs.append("沪深A过滤后为空，已回退使用未过滤实时快照（可能是代码格式/正则导致）。")
        s = s0.copy()

    s["sec_code"] = s["sec_code"].apply(canonical_code)
    s["change_pct"] = s["change_pct"].apply(safe_to_float)
    if "amount" in s.columns:
        s["amount"] = s["amount"].apply(safe_to_float)
        s["amount"], _ = scale_amount_series_to_yuan(s["amount"])
    else:
        s["amount"] = np.nan

    s = s.dropna(subset=["sec_code", "change_pct"])

    if s.empty:
        return None, qvix_info, msgs + ["AkShare 实时A股行情为空（过滤后无沪深A）。"]

    # compute intraday factors
    allA_ew_pct = float(s["change_pct"].mean())
    allA_ew_ret = allA_ew_pct / 100.0

    is_flat = np.isclose(s["change_pct"].fillna(0.0), 0.0)
    non_flat = s.loc[~is_flat]
    up_cnt = int((non_flat["change_pct"] > 0).sum())
    down_cnt = int((non_flat["change_pct"] < 0).sum())
    denom = up_cnt + down_cnt
    up_ratio = (up_cnt / denom) if denom > 0 else np.nan
    up_down_ratio = (up_cnt / down_cnt) if down_cnt > 0 else np.nan

    hot_excess = np.nan
    if s["amount"].notna().any():
        sub = s.nlargest(int(hot_top_n), "amount")
        hot_ret_pct = float(sub["change_pct"].mean()) if len(sub) else np.nan
        hot_excess = (hot_ret_pct - allA_ew_pct) / 100.0
    # style rel: zz1000 - hs300 from realtime index spot (fallback to min data)
    rel = np.nan
    hs_pct = np.nan
    zz_pct = np.nan
    try:
        idx_spot, idx_src = ak_fetch_index_spot_any()
        msgs.append(f"指数实时快照来源: {idx_src} | n={len(idx_spot)}")
        hs = idx_spot.loc[idx_spot["sec_code"] == CODE_HS300, "change_pct"]
        zz = idx_spot.loc[idx_spot["sec_code"] == CODE_ZZ1000, "change_pct"]
        if not hs.empty:
            hs_pct = float(hs.iloc[0])
        if not zz.empty:
            zz_pct = float(zz.iloc[0])
        if np.isfinite(hs_pct) and np.isfinite(zz_pct):
            diff_pct = float(zz_pct - hs_pct)
            rel = infer_rel_from_pct_diff(diff_pct)
            msgs.append(f"指数相对差值 raw={diff_pct} -> rel={rel:.6f}")
    except Exception as e:
        msgs.append(f"指数实时行情获取失败（将尝试Eastmoney直连/分钟线/ETF回退）：{e}")        # Extra fallback: Eastmoney push2 API (no AkShare)
        try:
            hs_pct = em_fetch_index_pct(CODE_HS300)
            zz_pct = em_fetch_index_pct(CODE_ZZ1000)
            if np.isfinite(hs_pct) and np.isfinite(zz_pct):
                diff_pct = float(zz_pct - hs_pct)
                rel = infer_rel_from_pct_diff(diff_pct)
                msgs.append(f"指数相对差值 raw={diff_pct} -> rel={rel:.6f}")
                msgs.append("指数实时快照来源: eastmoney push2 API")
        
        except Exception as _e2:
            msgs.append(f"Eastmoney直连回退失败：{_e2}")

    # Fallback: minute series for indices (use yesterday close from local DB if provided)
    if not np.isfinite(rel):
        try:
            hs_prev = np.nan
            zz_prev = np.nan
            if isinstance(index_prev_close, dict):
                hs_prev = safe_to_float(index_prev_close.get(CODE_HS300))
                zz_prev = safe_to_float(index_prev_close.get(CODE_ZZ1000))

            if (not np.isfinite(hs_pct)) and np.isfinite(hs_prev):
                hs_min = ak_fetch_index_min_em(CODE_HS300)
                ccol = _pick_df_col(hs_min, ["收盘", "close", "最新价", "收盘价"])
                if ccol is None:
                    for c in hs_min.columns[::-1]:
                        if pd.api.types.is_numeric_dtype(hs_min[c]):
                            ccol = c
                            break
                hs_now = float(pd.to_numeric(hs_min[ccol], errors="coerce").dropna().iloc[-1]) if ccol else np.nan
                if np.isfinite(hs_now) and hs_prev > 0:
                    hs_pct = (hs_now / hs_prev - 1.0) * 100.0

            if (not np.isfinite(zz_pct)) and np.isfinite(zz_prev):
                zz_min = ak_fetch_index_min_em(CODE_ZZ1000)
                ccol = _pick_df_col(zz_min, ["收盘", "close", "最新价", "收盘价"])
                if ccol is None:
                    for c in zz_min.columns[::-1]:
                        if pd.api.types.is_numeric_dtype(zz_min[c]):
                            ccol = c
                            break
                zz_now = float(pd.to_numeric(zz_min[ccol], errors="coerce").dropna().iloc[-1]) if ccol else np.nan
                if np.isfinite(zz_now) and zz_prev > 0:
                    zz_pct = (zz_now / zz_prev - 1.0) * 100.0

            if np.isfinite(hs_pct) and np.isfinite(zz_pct):
                diff_pct = float(zz_pct - hs_pct)
                rel = infer_rel_from_pct_diff(diff_pct)
                msgs.append(f"指数相对差值 raw={diff_pct} -> rel={rel:.6f}")
                msgs.append("小盘-大盘相对：使用指数分钟线 + 昨收(本地DB) 计算")
        except Exception as e:
            msgs.append(f"指数分钟线回退失败（将尝试ETF代理）：{e}")

    # Fallback: ETF proxy when index spot/min not available
    if not np.isfinite(rel):
        try:
            etf_spot = ak_fetch_etf_spot_em()
            hs_etf_pct = _etf_proxy_pct("510300", etf_spot)
            zz_etf_pct = _etf_proxy_pct("159845", etf_spot)
            if not np.isfinite(zz_etf_pct):
                zz_etf_pct = _etf_proxy_pct("512100", etf_spot)
            if np.isfinite(hs_etf_pct) and np.isfinite(zz_etf_pct):
                rel = float(zz_etf_pct - hs_etf_pct) / 100.0
                msgs.append("小盘-大盘相对：使用ETF(510300 vs 159845/512100) 涨跌幅代理")
        except Exception as e:
            msgs.append(f"ETF代理回退失败（小盘-大盘相对将缺失）：{e}")

    # L/E factors (post-close) -> use latest DB day values
    ref_row = d[d["trade_date"] == ref_dt].iloc[-1] if (d["trade_date"] == ref_dt).any() else latest_row
    etf_pct = safe_to_float(ref_row.get("etf_eq_share_pct"))
    turnover_rel_ma10 = safe_to_float(ref_row.get("turnover_rel_ma10"))

    # ---- compute sentiment score for today using SAME logic, but:
    # use intraday B/H/R; use yesterday L/E; scaling uses history comp min/max ----
    # Build history comp (unscaled)
    hist = d[d["trade_date"] < today_ts].set_index("trade_date") if has_today_in_db else d.set_index("trade_date")


    keys = [k for k in ["B", "L", "H", "R", "E"] if k in weights]
    if not keys:
        # still output row w/ NaN score
        row = {
            "trade_date": today_ts,
            "allA_ew_ret": allA_ew_ret,
            "up_cnt": up_cnt,
            "down_cnt": down_cnt,
            "up_ratio": up_ratio,
            "up_down_ratio": up_down_ratio,
            "hot_excess": hot_excess,
            "rel_zz1000_hs300": rel,
            "etf_eq_share_pct": etf_pct,
            "turnover_rel_ma10": turnover_rel_ma10,
            "sentiment_score": np.nan,
            "sentiment_mom": np.nan,
            "sentiment_z": np.nan,
        }
        return pd.Series(row), qvix_info, msgs + ["weights 未配置(B/L/H/R/E)，无法计算盘中情绪总分"]

    w_raw = {k: float(weights.get(k, 0.0)) for k in keys}

    # history series
    sB = hist["up_ratio"].astype(float)
    sL = hist["turnover_rel_ma10"].astype(float)
    sH = hist["hot_excess"].astype(float)
    sR = hist["rel_zz1000_hs300"].astype(float)
    sE = hist["etf_eq_share_pct"].astype(float)

    # zscores for history (for comp scaling)
    zB = zscore(sB, 20, 5)
    zL = zscore(sL, 20, 5)
    zH = zscore(sH, 20, 5)
    zR = zscore(sR, 20, 5)
    zE = zscore(sE, 20, 5)

    # history comp
    comp_hist = pd.Series(index=hist.index, dtype=float)
    for dti in comp_hist.index:
        num = 0.0
        den = 0.0
        for k in keys:
            v = {"B": zB, "L": zL, "H": zH, "R": zR, "E": zE}[k].get(dti, np.nan)
            if np.isfinite(v):
                num += float(w_raw.get(k, 0.0)) * float(v)
                den += float(w_raw.get(k, 0.0)) if float(w_raw.get(k, 0.0)) > 0 else abs(float(w_raw.get(k, 0.0)))
        comp_hist.loc[dti] = num / den if den > 0 else np.nan

    comp_vals = comp_hist[np.isfinite(comp_hist)]
    if comp_vals.empty:
        lo = hi = np.nan
    else:
        lo = float(np.nanmin(comp_vals.values))
        hi = float(np.nanmax(comp_vals.values))

    # today's zscores (append today's factor to compute rolling)
    def _z_today(series_hist: pd.Series, today_val: float) -> float:
        s_all = pd.concat([series_hist, pd.Series({today_ts: today_val})])
        return float(robust_zscore(s_all, window=60, min_periods=10).get(today_ts, np.nan))

    z_today = {
        "B": _z_today(sB, up_ratio),
        "L": _z_today(sL, turnover_rel_ma10),
        "H": _z_today(sH, hot_excess),
        "R": _z_today(sR, rel),
        "E": _z_today(sE, etf_pct),
    }

    num = 0.0
    den = 0.0
    for k in keys:
        v = z_today.get(k, np.nan)
        if np.isfinite(v):
            num += float(w_raw.get(k, 0.0)) * float(v)
            den += float(w_raw.get(k, 0.0)) if float(w_raw.get(k, 0.0)) > 0 else abs(float(w_raw.get(k, 0.0)))
    comp_today = num / den if den > 0 else np.nan

    # 盘中情绪总分：将加权Z合成 comp_today 映射为 0~100 百分制（稳定标尺，不使用滚动分位/minmax）
    scale = weights_scale(w_raw, keys)
    score_today = sigmoid_0_100(comp_today, scale)
    if np.isfinite(score_today):
        msgs.append(f"盘中情绪合成: comp_today={comp_today:+.3f}, scale={scale:.3f} -> score_today={score_today:.2f}")
    else:
        msgs.append("盘中情绪合成: score_today 无效（NaN）")


    # momentum & z (append today's score to history score series)
    score_hist = pd.to_numeric(hist["sentiment_score"], errors="coerce").astype(float)
    score_all = pd.concat([score_hist, pd.Series({today_ts: score_today})])
    mom_all = ema(score_all, 3) - ema(score_all, 10)
    z_mom_all = zscore(mom_all, 20, 5)

    row = {
        "trade_date": today_ts,
        "allA_ew_ret": allA_ew_ret,
        "up_cnt": up_cnt,
        "down_cnt": down_cnt,
        "up_ratio": up_ratio,
        "up_down_ratio": up_down_ratio,
        "hot_excess": hot_excess,
        "rel_zz1000_hs300": rel,
        "etf_eq_share_pct": etf_pct,
        "turnover_rel_ma10": turnover_rel_ma10,
        "sentiment_comp": comp_today,
        "sentiment_score": score_today,
        "sentiment_mom": float(mom_all.get(today_ts, np.nan)),
        "sentiment_z": float(z_mom_all.get(today_ts, np.nan)),
    }
    return pd.Series(row), qvix_info, msgs

def main():
    st.set_page_config(page_title="A股情绪 & 板块风险观察", layout="wide")
    st.title("A股情绪 & 板块风险观察")

    msgs_turnover: List[str] = []

    runtime_paths = build_runtime_paths(app_dir())
    ensure_runtime_dirs(runtime_paths)
    stock_db = runtime_paths.stock_db
    if not stock_db:
        st.error(f"未找到股票/指数DB：{DEFAULT_STOCK_DB_CANDIDATES}。请把DB放到当前目录。")
        return

    concept_db = runtime_paths.concept_db
    etf_db = runtime_paths.etf_db

    # Sidebar config（不改你已有参数命名风格）
    st.sidebar.header("参数")
    st.sidebar.subheader("情绪总分权重（默认=你的固定权重）")
    # 说明：情绪总分=0.35B+0.25L+0.15H+0.15R+0.10E（盘中缺因子会按可得权重重新归一）
    wB = st.sidebar.number_input("wB(广度: 上涨家数占比/涨跌家数比)", min_value=-5.0, max_value=5.0, value=0.35, step=0.05)
    wL = st.sidebar.number_input("wL(流动性: 成交额/MA10-1)", min_value=-5.0, max_value=5.0, value=0.25, step=0.05)
    wH = st.sidebar.number_input("wH(热度: 热股超额)", min_value=-5.0, max_value=5.0, value=0.15, step=0.05)
    wR = st.sidebar.number_input("wR(风格: 小盘-大盘相对)", min_value=-5.0, max_value=5.0, value=0.15, step=0.05)
    wE = st.sidebar.number_input("wE(资金: 权益ETF份额日增幅%)", min_value=-5.0, max_value=5.0, value=0.10, step=0.05)
    hot_top = st.sidebar.number_input("热股TopN(按成交额)", min_value=20, max_value=300, value=100, step=10)
    lookback = st.sidebar.number_input("日线回看(交易日)", min_value=40, max_value=120, value=60, step=10)
    theme_topk = st.sidebar.number_input("题材TopK", min_value=10, max_value=80, value=30, step=5)

    st.sidebar.subheader("数据日期切换")
    tz_offset = st.sidebar.number_input("北京时间偏移(UTC+?)", min_value=-12, max_value=14, value=8, step=1)
    now_cn = _now_with_tz_offset(int(tz_offset))

    remote_bundle = discover_data_dates_bundle(concept_db if (concept_db and os.path.exists(concept_db)) else None,
                                               tz_offset_hours=int(tz_offset))
    st.session_state["remote_bundle"] = remote_bundle

    t1_close = remote_bundle.get("t1_close_day") or remote_bundle.get(
        "remote_latest_stock_close_day") or remote_bundle.get("calendar_last_trade_day")
    t2_close = remote_bundle.get("t2_close_day")

    is_trade_day = (_today_trade_date_cn(now_cn, int(tz_offset)) is not None)
    in_snapshot_window = bool(is_trade_day and _is_snapshot_prefer_window_cn(now_cn, int(tz_offset)))

    today_str = now_cn.strftime("%Y-%m-%d")
    opt_snapshot = f"盘中快照（{today_str}）"
    opt_t1 = f"T-1 收盘（{t1_close}）" if t1_close else "T-1 收盘（不可用）"
    opt_t2 = f"T-2 收盘（{t2_close}）" if t2_close else "T-2 收盘（不可用）"

    default_idx = 0 if in_snapshot_window else 1
    choice = st.sidebar.radio("查看数据日期", [opt_snapshot, opt_t1, opt_t2], index=default_idx)

    if choice == opt_snapshot and (not in_snapshot_window):
        st.sidebar.warning("当前非交易日或不在 09:30–17:30 时间窗内：盘中快照不可用，将回退到 T-1 收盘口径。")
        effective_view = "t1"
    elif (choice == opt_t2) and (not t2_close):
        st.sidebar.warning("无法确定 T-2 交易日：将回退到 T-1 收盘口径。")
        effective_view = "t1"
    else:
        effective_view = "snapshot" if (choice == opt_snapshot) else ("t2" if (choice == opt_t2) else "t1")

    effective_mode = "snapshot" if (effective_view == "snapshot") else "close"
    requested_close_day = t2_close if (effective_view == "t2") else t1_close
    snapshot_trade_dt = pd.Timestamp(now_cn.date()) if (effective_view == "snapshot") else None

    use_intraday = bool(effective_mode == "snapshot")
    use_intraday_theme = bool(effective_mode == "snapshot")
    force_intraday = False

    st.sidebar.caption(
        f"接口最新收盘：stock={remote_bundle.get('remote_latest_stock_close_day')}｜concept={remote_bundle.get('remote_latest_concept_close_day')}｜"
        f"aligned(T-1)={t1_close}｜T-2={t2_close or 'N/A'}"
    )

    st.sidebar.caption("说明：选择“最近收盘”将禁用所有盘中快照；选择“盘中快照优先”则尽可能使用快照并对不可取字段回退到最近收盘。")

    # --- 启动时补齐缺失数据（只在本次会话启动时跑一次）---
    st.sidebar.subheader("数据库追平（对齐接口最新收盘）")
    auto_catchup = st.sidebar.checkbox("自动追平（DB落后时自动更新）", value=True)
    force_backfill = st.sidebar.checkbox("强制全量 backfill（60日）", value=False)
    show_catchup_log = st.sidebar.checkbox("显示追平日志", value=False)

    local_before = get_local_last_dates(stock_db, concept_db, etf_db)
    remote_stock_day = remote_bundle.get("remote_latest_stock_close_day") or remote_bundle.get(
        "calendar_last_trade_day")
    remote_concept_day = remote_bundle.get("remote_latest_concept_close_day") or remote_stock_day
    remote_etf_day = remote_bundle.get("remote_latest_etf_close_day") or remote_stock_day

    need_sync = False
    if is_date_behind(local_before.get("stock"), remote_stock_day) or is_date_behind(local_before.get("index"),
                                                                                     remote_stock_day):
        need_sync = True
    if concept_db and os.path.exists(concept_db) and is_date_behind(local_before.get("concept"), remote_concept_day):
        need_sync = True
    if etf_db and os.path.exists(etf_db) and is_date_behind(local_before.get("etf"), remote_etf_day):
        need_sync = True

    st.sidebar.caption(
        f"本地最新：stock={local_before.get('stock')}｜index={local_before.get('index')}｜concept={local_before.get('concept')}｜etf={local_before.get('etf')}"
    )

    if "auto_catchup_state" not in st.session_state:
        st.session_state["auto_catchup_state"] = {"target": None, "ok": False, "ts": 0.0}
    if "catchup_log" not in st.session_state:
        st.session_state["catchup_log"] = []

    target_key = f"{remote_stock_day}|{remote_concept_day}|{remote_etf_day}"
    now_ts = float(dt.datetime.utcnow().timestamp())
    state = st.session_state.get("auto_catchup_state") or {}
    throttled = bool(
        (state.get("target") == target_key)
        and (now_ts - float(state.get("ts") or 0.0) < 300.0)
        and bool(state.get("ok") or (not force_backfill))
    )

    if (force_backfill or (auto_catchup and need_sync)) and (not throttled):
        with st.spinner("正在追平数据库到接口最新收盘日..."):
            local_after, logs = maybe_backfill_all(stock_db, concept_db, etf_db, force=bool(force_backfill))
        st.session_state["catchup_log"] = (logs or [])[-200:]

        local2 = get_local_last_dates(stock_db, concept_db, etf_db)
        still_behind = False
        if is_date_behind(local2.get("stock"), remote_stock_day) or is_date_behind(local2.get("index"),
                                                                                   remote_stock_day):
            still_behind = True
        if concept_db and os.path.exists(concept_db) and is_date_behind(local2.get("concept"), remote_concept_day):
            still_behind = True
        if etf_db and os.path.exists(etf_db) and is_date_behind(local2.get("etf"), remote_etf_day):
            still_behind = True

        st.session_state["auto_catchup_state"] = {"target": target_key, "ok": (not still_behind), "ts": now_ts}

    if show_catchup_log and st.session_state.get("catchup_log"):
        with st.sidebar.expander("追平日志", expanded=False):
            for m in st.session_state.get("catchup_log", [])[-120:]:
                st.write(m)

        # --- 数据库日线更新（adata/baostock；仅日线接口写库） ---
    st.sidebar.subheader("数据库日线更新（adata/baostock）")
    auto_daily_sync = st.sidebar.checkbox("盘后自动：使用日线接口更新DB并重算", value=True)
    validate_db_btn = st.sidebar.button("核验DB最新交易日数据")
    manual_daily_sync = st.sidebar.button("手动更新DB（日线接口）")
    repair_latest_btn = st.sidebar.button("修复最新交易日（日线接口，覆写当日数据）")

    if "daily_sync_log" not in st.session_state:
        st.session_state["daily_sync_log"] = []
    if "db_validate_result" not in st.session_state:
        st.session_state["db_validate_result"] = None

    # On-demand validation
    if validate_db_btn:
        st.session_state["db_validate_result"] = validate_db_latest_day(
            stock_db=stock_db, concept_db=concept_db, tz_offset_hours=int(tz_offset)
        )

    # Manual daily update (no snapshot write)
    if manual_daily_sync:
        with st.spinner("正在使用日收盘接口更新数据库（baostock/adata）..."):
            local_after, logs = maybe_backfill_all(stock_db, concept_db, etf_db, force=True)
        st.session_state["daily_sync_log"] = logs
        st.session_state["db_validate_result"] = validate_db_latest_day(
            stock_db=stock_db, concept_db=concept_db, tz_offset_hours=int(tz_offset)
        )
        _st_rerun_safe()

    # Repair latest day by delete + re-backfill (daily only)
    if repair_latest_btn:
        with st.spinner("正在修复最新交易日数据：删除当日行并重拉（日线接口）..."):
            ok, logs, val = repair_latest_trade_day_daily(
                stock_db=stock_db, concept_db=concept_db, etf_db=etf_db, tz_offset_hours=int(tz_offset)
            )
        st.session_state["daily_sync_log"] = logs
        st.session_state["db_validate_result"] = val
        if ok:
            _st_rerun_safe()

    # Auto: once per trading day after close
    try:
        if auto_daily_sync:
            now_cn = _now_with_tz_offset(int(tz_offset))
            td = _today_trade_date_cn(now_cn, int(tz_offset))
            if td is not None and _is_after_close_cn(now_cn, int(tz_offset)):
                key = f"auto_daily_sync_done_{td.strftime('%Y-%m-%d')}"
                if not st.session_state.get(key, False):
                    local_after, logs = maybe_backfill_all(stock_db, concept_db, etf_db, force=False)
                    st.session_state[key] = True
                    st.session_state["daily_sync_log"] = logs
                    st.session_state["db_validate_result"] = validate_db_latest_day(
                        stock_db=stock_db, concept_db=concept_db, tz_offset_hours=int(tz_offset)
                    )
                    _st_rerun_safe()
    except Exception:
        pass

    with st.sidebar.expander("日线更新 / 核验日志", expanded=False):
        # validation summary
        val = st.session_state.get("db_validate_result", None)
        if isinstance(val, dict):
            if val.get("ok", False):
                st.success(f"DB核验通过（expected_day={val.get('expected_day')}）")
            else:
                st.error(f"DB核验未通过（expected_day={val.get('expected_day')}）")
                for s in val.get("issues", [])[:12]:
                    st.write("-", s)
        # update logs
        for line in st.session_state.get("daily_sync_log", [])[-120:]:
            st.write(line)


    weights = {"B": float(wB), "L": float(wL), "H": float(wH), "R": float(wR), "E": float(wE)}

    with st.spinner("读取本地DB..."):
        # stock/index
        conS = connect(stock_db)
        try:
            if not table_exists(conS, "kline_daily"):
                st.error(f"{stock_db} 中不存在表 kline_daily")
                return

            dates = last_n_trade_dates(conS, "kline_daily", "trade_date", int(lookback))
            if len(dates) < 5:
                st.error("本地 kline_daily 可用交易日过少，无法计算。")
                return

            stock_df = load_stock_cross_section(conS, dates)
            index_df = load_index_series(conS, [CODE_SH000001, CODE_SZ399001, CODE_HS300, CODE_ZZ1000], dates)
            # market turnover denominator (per spec: ALWAYS prefer indices 000001+399001; fill missing days with stock sum)
            market_turnover = build_market_turnover(index_df=index_df, stock_df=stock_df, dates=dates, msgs=msgs_turnover)
            latest_dt = dates[-1]

            # 仅用 000001+399001 构建的“严格分母”（不使用 stock sum 修补），供“题材成交额占比”等指标使用
            try:
                market_turnover_idx_only_raw = compute_market_turnover(index_df)
                market_turnover_idx_only, f_idx_only = scale_amount_series_to_yuan(market_turnover_idx_only_raw)
                msgs_turnover.append(f"严格分母(000001+399001)成交额缩放因子={f_idx_only:g}")
            except Exception:
                market_turnover_idx_only = market_turnover
        finally:
            conS.close()

        # etf
        etf_df = load_etf_total_df(etf_db) if etf_db else pd.DataFrame(columns=["trade_date","sse_share_total","szse_share_total","total_share"])

        # concept
        concept_df = pd.DataFrame()
        name_map = {}
        if concept_db and os.path.exists(concept_db):
            conC = connect(concept_db)
            try:
                if table_exists(conC, "concept_kline"):
                    concept_df = load_concept_kline(conC, dates, "concept_kline")
                    name_map = load_concept_name_map(conC)
                elif table_exists(conC, "concept_kline_ths"):
                    concept_df = load_concept_kline(conC, dates, "concept_kline_ths")
                    name_map = load_concept_name_map(conC)
                else:
                    st.warning(f"{concept_db} 中未找到 concept_kline / concept_kline_ths，题材面板将缺失。")
            finally:
                conC.close()
        else:
            st.warning("未找到 ths_concept.db，题材面板将缺失。")

    
    # ---- 口径与对齐：用于保证“成交额/占比”等指标的分母分子来自同一时点 ----
    now_cn2 = _now_with_tz_offset(int(tz_offset))
    today_ts = pd.Timestamp(now_cn2.date())
    close_ref_dt_cal = last_completed_trade_date_cn(now_cn2, int(tz_offset))
    requested_close_dt0 = pd.Timestamp(requested_close_day) if requested_close_day else close_ref_dt_cal

    # DB 侧可用的最新日期（分别对 stock/index/concept）
    db_latest_stock_dt = pd.Timestamp(latest_dt) if latest_dt is not None else today_ts
    try:
        _idx_max = index_df["trade_date"].apply(normalize_date).max() if (index_df is not None and not index_df.empty and "trade_date" in index_df.columns) else db_latest_stock_dt
        db_latest_index_dt = pd.Timestamp(_idx_max) if _idx_max is not None else db_latest_stock_dt
    except Exception:
        db_latest_index_dt = db_latest_stock_dt
    try:
        _c_max = concept_df["trade_date"].apply(normalize_date).max() if (concept_df is not None and not concept_df.empty and "trade_date" in concept_df.columns) else db_latest_stock_dt
        db_latest_concept_dt = pd.Timestamp(_c_max) if _c_max is not None else db_latest_stock_dt
    except Exception:
        db_latest_concept_dt = db_latest_stock_dt

    # 为保证跨表一致性：收盘口径统一使用“各数据表都覆盖到的最近日期”
    panel_close_dt = min([d for d in [requested_close_dt0, close_ref_dt_cal, db_latest_stock_dt, db_latest_index_dt, db_latest_concept_dt] if d is not None])

    # 盘中快照的目标交易日：仅在交易日使用“今天”，否则回退到 panel_close_dt
    snapshot_trade_dt = today_ts if _today_trade_date_cn(now_cn2, int(tz_offset)) is not None else None
# ---- Sentiment ----
    # st.subheader("指标计算（本地DB → 日线）")
    # st.markdown(f"**DB最新日线日期（以stock日线为准）**：{latest_dt.date()}")
    with st.expander("数据核验（成交额/索引）", expanded=False):
        try:
            st.write("当前口径：", "盘中快照" if effective_mode == "snapshot" else "最近收盘")
            st.write("panel_close_dt：", str(panel_close_dt.date()) if panel_close_dt is not None else None)
            st.write("snapshot_trade_dt：", str(snapshot_trade_dt.date()) if snapshot_trade_dt is not None else None)

            # —— close 口径：同一天 000001 / 399001 成交额（原始+缩放到元）——
            idx_norm = index_df.copy()
            idx_norm["trade_date"] = idx_norm["trade_date"].apply(normalize_date)
            idx_norm["sec_code"] = idx_norm["sec_code"].apply(canonical_code)

            sub = idx_norm[
                (idx_norm["trade_date"] == panel_close_dt)
                & (idx_norm["sec_code"].isin([CODE_SH000001, CODE_SZ399001]))
            ].copy()

            if not sub.empty and "amount" in sub.columns:
                raw = sub.set_index("sec_code")["amount"].apply(safe_to_float)
                st.write("close_raw_index_amount：", raw.to_dict())

                scaled, f = scale_amount_series_to_yuan(raw)
                scaled_vals = pd.to_numeric(scaled, errors="coerce").values
                st.write("close_amount_scale_factor：", float(f))
                st.write(
                    "close_scaled_index_amount(元)：",
                    {k: (float(v) if np.isfinite(v) else None) for k, v in zip(raw.index.tolist(), scaled_vals.tolist())},
                )
                st.write("close_market_turnover_sum(元)：", float(np.nansum(scaled_vals)))
            else:
                st.write("close_index_amount：", "N/A（index_df 里未找到 panel_close_dt 当天的 000001/399001 成交额）")

            # —— market_turnover 最终用于面板的数值（元）——
            try:
                v_final = market_turnover.loc[panel_close_dt] if panel_close_dt in market_turnover.index else np.nan
                st.write("market_turnover_used(元)：", float(v_final) if np.isfinite(v_final) else None)
            except Exception:
                st.write("market_turnover_used(元)：", None)

            # —— 严格分母：仅 000001+399001（元）——
            try:
                v_strict = market_turnover_idx_only.loc[panel_close_dt] if panel_close_dt in market_turnover_idx_only.index else np.nan
                st.write("market_turnover_strict(元)：", float(v_strict) if np.isfinite(v_strict) else None)
            except Exception:
                st.write("market_turnover_strict(元)：", None)

            # —— concept 当天成交额（原始口径统计，便于肉眼判断单位）——
            if concept_df is not None and (not concept_df.empty) and ("amount" in concept_df.columns) and ("trade_date" in concept_df.columns):
                ctmp = concept_df.copy()
                ctmp["trade_date"] = ctmp["trade_date"].apply(normalize_date)
                ca = ctmp[ctmp["trade_date"] == panel_close_dt]["amount"].apply(safe_to_float)
                if not ca.empty:
                    st.write("concept_amount_raw_median：", float(ca.median()) if np.isfinite(ca.median()) else None)
                    st.write("concept_amount_raw_p95：", float(ca.quantile(0.95)) if np.isfinite(ca.quantile(0.95)) else None)
                    st.write("concept_amount_raw_max：", float(ca.max()) if np.isfinite(ca.max()) else None)
        except Exception as e:
            st.write("核验信息读取失败：", e)


    daily, msgs = compute_daily_sentiment(stock_df, index_df, etf_df, dates, weights, int(hot_top))
    # intraday snapshot placeholders (defined early to avoid NameError)
    intraday_row = None
    intraday_msgs: List[str] = []
    if msgs_turnover:
        # merge diagnostics (dedup)
        for mm in msgs_turnover:
            if mm and mm not in msgs:
                msgs.append(mm)
    daily = daily.sort_values("trade_date")

    # export last 40
    try:
        daily.tail(40).to_csv(runtime_paths.metrics_csv, index=False, encoding="utf-8-sig")
    except Exception as e:
        st.warning(f"写出 {runtime_paths.metrics_csv} 失败：{e}")


    # 额外输出：保存到本程序同目录（方便其他脚本读取）
    try:
        daily.tail(40).to_csv(runtime_paths.legacy_metrics_csv, index=False, encoding="utf-8-sig")
    except Exception:
        pass

    # 同步写入 daily_matrics 表（存在则 upsert；不存在则创建/补列）
    try:
        con_metrics = connect(stock_db)
        _ = upsert_daily_matrics(con_metrics, daily)
        con_metrics.close()
    except Exception as e:
        st.warning(f"写入 daily_matrics 失败：{e}")

    if msgs:
        with st.expander("缺失/提示", expanded=True):
            for m in msgs:
                st.write(f"- {m}")

    if intraday_msgs:
        with st.expander("盘中快照提示", expanded=False):
            for m in intraday_msgs:
                st.write(f"- {m}")

    if daily.empty:
        st.error("情绪指标计算失败（无可用日线）。")
        return

    # DB 收盘口径：优先使用 panel_close_dt 对齐（避免与题材/指数日期不一致）
    try:
        _hit = daily[daily["trade_date"] == panel_close_dt]
        last_db = _hit.iloc[-1] if not _hit.empty else daily.iloc[-1]
    except Exception:
        last_db = daily.iloc[-1]
    last = last_db
    last = last_db
    intraday_row = None
    qvix_info = {'qvix': np.nan, 'qvix_pctile': np.nan}
    intraday_msgs: List[str] = []
    # ---- Intraday snapshot (optional) ----
    try:
        now_cn = _now_with_tz_offset(int(tz_offset))
        today_ts = pd.Timestamp(now_cn.date())
        has_today_in_db = bool((daily['trade_date'] == today_ts).any())
        is_trade = is_cn_trading_time(now_cn, int(tz_offset))
        # 启用规则：交易时段启用；非交易时段若DB缺少当日数据则启用；force_intraday 则无条件启用
        should_intraday = bool(use_intraday and (effective_mode == 'snapshot'))

        if should_intraday:
            # 准备指数昨收（用于指数分钟线回退计算当日涨跌幅）
            idx_prev_close = {}
            try:
                ref_dt_main = dates[-1]
                if has_today_in_db and len(dates) >= 2:
                    ref_dt_main = dates[-2]
                tmp = index_df.copy()
                tmp["trade_date"] = tmp["trade_date"].apply(normalize_date)
                tmp["sec_code"] = tmp["sec_code"].astype(str).apply(canonical_code)
                for _c in [CODE_HS300, CODE_ZZ1000]:
                    hit = tmp[(tmp["sec_code"] == _c) & (tmp["trade_date"] == ref_dt_main)]
                    if not hit.empty:
                        idx_prev_close[_c] = float(hit["close"].apply(safe_to_float).dropna().iloc[-1])
            except Exception:
                idx_prev_close = {}


            intraday_row, qvix_info, intraday_msgs = compute_intraday_snapshot_row(

                daily,

                weights,

                int(hot_top),

                int(tz_offset),

                index_prev_close=idx_prev_close,

                allow_override_db_today=True,

            )

            if intraday_row is not None:

                last = intraday_row

                # st.markdown(f"**盘中快照日期**：{today_ts.date()}（临时快照，不写入DB；盘后请用更新脚本覆盖当日收盘数据）")

            else:

                intraday_msgs.append("盘中快照未生成（可能：AkShare字段变化/过滤后无沪深A/接口暂不可用）。")

            # 仅在 intraday_row 生成成功时落地一个“盘中接口面”
            try:
                def _to_float_or_nan(x):
                    try:
                        s = str(x).strip().replace(",", "")
                        if s == "" or s.lower() == "none":
                            return np.nan
                        return float(s)
                    except Exception:
                        return np.nan

                out_dir = app_dir()  # 你原程序已有
                fp = os.path.join(out_dir, "intraday_metrics_latest.csv")

                df_rt = pd.DataFrame([{
                    "ts": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "trade_date": str(intraday_row.get("trade_date", "")),
                    "sentiment_score": _to_float_or_nan(intraday_row.get("sentiment_score")),
                    "sentiment_z": _to_float_or_nan(intraday_row.get("sentiment_z")),
                    "source": "streamlit_panel",}])
                df_rt.to_csv(fp, index=False, encoding="utf-8-sig")
            except Exception:
                pass
        else:
            if use_intraday:
                intraday_msgs.append(f"当前北京时间 {now_cn.strftime('%H:%M:%S')} 非交易时段且DB已有当日数据，盘中快照不启用")
    except Exception as e:
        intraday_msgs.append(f"盘中快照计算异常：{e}")
    
    # ---- QVIX (display only; always compute) ----
    try:
        # Try intraday first (may be all None near close); fallback to daily close.
        qmin = ak_fetch_500etf_qvix_min()
        qv_now = _qvix_value_from_df(qmin)
        if not np.isfinite(qv_now):
            qdaily = ak_fetch_500etf_qvix_daily()
            qv_now = _qvix_value_from_df(qdaily)
        if np.isfinite(qv_now):
            qvix_info['qvix'] = float(qv_now)
            try:
                qdaily = ak_fetch_500etf_qvix_daily()
                dcol = _pick_df_col(qdaily, ["QVIX","qvix","value","close","收盘","最新价"])
                if dcol is not None:
                    qvix_info['qvix_pctile'] = float(_qvix_pctile(qv_now, qdaily[dcol]))
            except Exception:
                pass
    except Exception as e:
        # keep NaN if failed
        intraday_msgs.append(f"QVIX 获取失败：{e}")

    with st.expander("QVIX 调试", expanded=False):
        st.write({
            "qvix": None if not np.isfinite(safe_to_float(qvix_info.get('qvix'))) else float(qvix_info.get('qvix')),
            "qvix_pctile": None if not np.isfinite(safe_to_float(qvix_info.get('qvix_pctile'))) else float(qvix_info.get('qvix_pctile')),
        })
        try:
            dfm = ak_fetch_500etf_qvix_min()
            st.write("min cols:", list(dfm.columns))
            st.dataframe(dfm.tail(5), width='stretch')
        except Exception as e:
            st.write("min fetch error:", str(e))
        try:
            dfd = ak_fetch_500etf_qvix_daily()
            st.write("daily cols:", list(dfd.columns))
            st.dataframe(dfd.tail(5), width='stretch')
        except Exception as e:
            st.write("daily fetch error:", str(e))

    
    # ---- Intraday runtime messages (after snapshot & QVIX) ----
    if intraday_msgs:
        with st.expander("盘中快照提示（运行信息）", expanded=False):
            for m in intraday_msgs:
                if m:
                    st.write(f"- {m}")

    try:
        now_cn2 = _now_with_tz_offset(int(tz_offset))
        is_trade2 = is_cn_trading_time(now_cn2, int(tz_offset))
        st.caption(
            f"北京时间 {now_cn2.strftime('%Y-%m-%d %H:%M:%S')} ｜交易时段: {'是' if is_trade2 else '否'} ｜盘中快照: {'启用' if (intraday_row is not None) else '未启用'}"
        )
    except Exception:
        pass

    st.caption("标记说明：🟢=盘中实时快照；(昨)=使用昨日收盘/最新日线；—=取值失败或缺数据（详见上方提示）")


    c1, c2, c3, c4, c5 = st.columns(5)
    rt_prefix = '🟢' if (intraday_row is not None) else ''
    c1.metric("情绪总分（0-100）", f"{safe_to_float(last.get('sentiment_score')):.2f}" if np.isfinite(safe_to_float(last.get("sentiment_score"))) else "—")
    c2.metric("动量(EMA3-EMA10)", f"{safe_to_float(last.get('sentiment_mom')):.2f}" if np.isfinite(safe_to_float(last.get("sentiment_mom"))) else "—")
    c3.metric(f"{rt_prefix}动量Z", f"{safe_to_float(last.get('sentiment_z')):.2f}" if np.isfinite(safe_to_float(last.get("sentiment_z"))) else "—")
    upc = safe_to_float(last.get("up_cnt"))
    dnc = safe_to_float(last.get("down_cnt"))
    if np.isfinite(upc) and np.isfinite(dnc):
        c4.metric(f"{rt_prefix}涨跌家数比(涨:跌)", f"{int(upc)}:{int(dnc)}")
    else:
        c4.metric(f"{rt_prefix}涨跌家数比(涨:跌)", "—")
    c5.metric(f"{rt_prefix}全A等权涨跌幅", f"{safe_to_float(last.get('allA_ew_ret'))*100:.2f}%" if np.isfinite(safe_to_float(last.get("allA_ew_ret"))) else "—")

    c6, c7, c8, c9, c10 = st.columns(5)
    # 用 QVIX 替换“上涨家数占比”展示（不改变情绪总分计算逻辑：总分仍使用 up_ratio 作为广度因子B）
    qv = safe_to_float(qvix_info.get("qvix"))
    qp = safe_to_float(qvix_info.get("qvix_pctile"))
    if np.isfinite(qv) and np.isfinite(qp):
        c6.metric(f"{rt_prefix}500ETF QVIX / 分位", f"{qv:.2f}  ({qp*100:.0f}%)")
    elif np.isfinite(qv):
        c6.metric(f"{rt_prefix}500ETF QVIX", f"{qv:.2f}")
    else:
        c6.metric(f"{rt_prefix}500ETF QVIX / 分位", "—")
    c7.metric(f"{rt_prefix}热股超额(Top热-全A等权)", f"{safe_to_float(last.get('hot_excess'))*100:.2f}%" if np.isfinite(safe_to_float(last.get("hot_excess"))) else "—")
    c8.metric(f"{rt_prefix}小盘-大盘相对(1000-300)", f"{safe_to_float(last.get('rel_zz1000_hs300'))*100:.2f}%" if np.isfinite(safe_to_float(last.get("rel_zz1000_hs300"))) else "—")
    etf_pct = safe_to_float(last.get("etf_eq_share_pct"))
    c9_label = "权益ETF份额日增幅" + ("(昨)" if (intraday_row is not None) else "")
    c9.metric(c9_label, f"{etf_pct*100:.2f}%" if np.isfinite(etf_pct) else "—")
    c10_label = "成交额/MA10-1" + ("(昨)" if (intraday_row is not None) else "")
    c10.metric(c10_label, f"{safe_to_float(last.get('turnover_rel_ma10'))*100:.2f}%" if np.isfinite(safe_to_float(last.get("turnover_rel_ma10"))) else "—")

    st.markdown("### 情绪总分（日线）")
    plot_df = daily[["trade_date", "sentiment_score"]].dropna().copy()
    # append/replace intraday point for visual reference (still date-based, no intraday axis)
    if intraday_row is not None and np.isfinite(safe_to_float(intraday_row.get("sentiment_score"))):
        try:
            td = intraday_row.get("trade_date")
            # drop existing same-day point (DB may already have a today row)
            plot_df = plot_df[plot_df["trade_date"] != td].copy()
            tmp = pd.DataFrame([{"trade_date": td, "sentiment_score": safe_to_float(intraday_row.get("sentiment_score"))}])
            plot_df = pd.concat([plot_df, tmp], ignore_index=True)
        except Exception:
            pass
    if len(plot_df) >= 2:
        st.line_chart(plot_df.set_index("trade_date")["sentiment_score"].tail(40))
    else:
        st.info("情绪总分可用日线不足（需更多历史）。")

    # ---- Theme Cycle ----
    st.markdown("---")
    st.subheader("指标体系B：同花顺概念板块（Theme Cycle）")
    if concept_df.empty:
        st.warning("题材数据缺失：concept_kline_ths 为空或不可用。")
        return


    # exclude concepts listed in filtered_concept.csv
    filtered_map = read_filtered_concepts_csv()
    filtered_codes = set(filtered_map.keys())
    if filtered_codes and (not concept_df.empty):
        # normalize concept_code to pure 6-digit string to avoid float/int mismatch like '886107.0'
        _code_norm = concept_df["concept_code"].astype(str).str.replace(r"\D", "", regex=True).str.zfill(6)
        concept_df = concept_df.loc[~_code_norm.isin(filtered_codes)].copy()
        # keep name_map consistent
        for k in list(name_map.keys()):
            if str(k) in filtered_codes:
                name_map.pop(k, None)

    # ---- 题材面板：按口径统一（最近收盘 vs 盘中快照）----
    concept_df_for_theme = concept_df
    market_turnover_for_theme = market_turnover_idx_only
    theme_latest_dt = panel_close_dt
    rt_theme_msgs: List[str] = []
    rt_turnover_diag: Dict[str, Any] = {}

    # Only attempt snapshot when requested and 'today' is a trading day
    if use_intraday_theme and (effective_mode == "snapshot") and (snapshot_trade_dt is not None):
        today_ts = snapshot_trade_dt

        concept_ok = False
        mkt_ok = False

        # 1) 概念实时快照（含成交额/涨跌幅）
        try:
            rt_con = normalize_rt_concept_df(adata_fetch_concept_current_ths())
            if not rt_con.empty:
                rt_theme_msgs.append(f"概念实时快照(adata) n={len(rt_con)}")

                # prev close per concept from the close reference day (panel_close_dt)
                _base = concept_df.copy()
                _base["trade_date"] = _base["trade_date"].apply(normalize_date)

                prev = _base[_base["trade_date"] == panel_close_dt][["concept_code", "close"]].copy()
                prev["concept_code"] = prev["concept_code"].apply(canonical_code)
                prev = prev.groupby("concept_code")["close"].last().reset_index().rename(columns={"close": "prev_close"})
                rt_con = rt_con.merge(prev, on="concept_code", how="left")

                rt_con["ret_1d"] = rt_con["change_pct"].apply(safe_to_float) / 100.0
                rt_con["close"] = rt_con["prev_close"].apply(safe_to_float) * (1.0 + rt_con["ret_1d"])
                rt_today = rt_con[["concept_code", "close", "change_pct", "amount"]].copy()
                rt_today["trade_date"] = today_ts
                rt_today = rt_today.dropna(subset=["concept_code", "close"])

                if not rt_today.empty:
                    concept_df_for_theme = _base[_base["trade_date"] != today_ts].copy()
                    concept_df_for_theme = pd.concat([concept_df_for_theme, rt_today], ignore_index=True)
                    concept_ok = True
        except Exception as e:
            rt_theme_msgs.append(f"概念实时快照(adata)失败: {e}")

        # 2) 指数实时快照（用于全市场成交额：000001+399001）
        try:
            rt_idx = normalize_rt_index_df(adata_fetch_index_current())
            if not rt_idx.empty:
                rt_theme_msgs.append(f"指数实时快照(adata) n={len(rt_idx)}")
                use_idx = rt_idx[rt_idx["sec_code"].isin([CODE_SH000001, CODE_SZ399001])].copy()
                if not use_idx.empty:
                    # raw amount diagnostics (before unit scaling)
                    for _, r in use_idx.iterrows():
                        rt_turnover_diag[str(r.get("sec_code", ""))] = safe_to_float(r.get("amount"))

                    if use_idx["amount"].notna().any():
                        amt_scaled, f_amt = scale_amount_series_to_yuan(use_idx["amount"])
                        rt_turnover_diag["amount_scale_factor"] = float(f_amt)
                        rt_turnover_diag["scaled_amounts"] = {
                            str(c): (float(v) if np.isfinite(v) else None)
                            for c, v in zip(use_idx["sec_code"].tolist(), pd.to_numeric(amt_scaled, errors="coerce").tolist())
                        }
                        mkt_amt = float(np.nansum(pd.to_numeric(amt_scaled, errors="coerce").values))
                        if np.isfinite(mkt_amt) and mkt_amt > 0:
                            market_turnover_for_theme = market_turnover_idx_only.copy()
                            market_turnover_for_theme.loc[today_ts] = mkt_amt
                            rt_theme_msgs.append(f"盘中全市场成交额(000001+399001)={mkt_amt/1e8:.1f}亿")
                            mkt_ok = True
        except Exception as e:
            rt_theme_msgs.append(f"指数实时快照(adata)失败: {e}")

        # Consistency rule: if any critical input missing, fallback to the same close day for both numerator & denominator.
        if concept_ok and mkt_ok:
            theme_latest_dt = today_ts
        else:
            concept_df_for_theme = concept_df
            market_turnover_for_theme = market_turnover_idx_only
            theme_latest_dt = panel_close_dt
            if not concept_ok:
                rt_theme_msgs.append("题材面板快照回退：概念成交额/涨跌幅快照不可用 → 使用最近收盘口径")
            if not mkt_ok:
                rt_theme_msgs.append("题材面板快照回退：000001/399001 成交额快照不可用 → 使用最近收盘口径")
    else:
        theme_latest_dt = panel_close_dt

    theme_tbl, theme_msgs = compute_theme_panel(concept_df_for_theme, name_map, market_turnover_for_theme, theme_latest_dt, int(theme_topk))
    theme_msgs = (rt_theme_msgs or []) + (theme_msgs or [])
    if theme_msgs:

        with st.expander("题材提示", expanded=False):
            for m in theme_msgs:
                st.write(f"- {m}")

    if theme_tbl.empty:
        st.warning("题材面板无可展示数据（可能最新日概念行情缺失）。")
        return

    show = theme_tbl.rename(
        columns={
            "concept_code": "概念代码",
            "name": "概念名称",
            "temperature": "Temperature（0-100）",
            "risk": "Risk（0-100）",
            "ret_1d": "涨跌幅(%)",
            "ret_5d": "近5日(%)",
            "amount_share_pct": "当日成交额占比(%)",
        }
    ).copy()

    for c in ["Temperature（0-100）", "Risk（0-100）"]:
        show[c] = show[c].map(lambda x: f"{safe_to_float(x):.1f}" if np.isfinite(safe_to_float(x)) else "—")
    for c in ["涨跌幅(%)", "近5日(%)", "当日成交额占比(%)"]:
        show[c] = show[c].map(lambda x: f"{safe_to_float(x):.2f}" if np.isfinite(safe_to_float(x)) else "—")

    st.dataframe(
        show[["概念代码", "概念名称", "Temperature（0-100）", "Risk（0-100）", "涨跌幅(%)", "近5日(%)", "当日成交额占比(%)"]],
        width='stretch',
        hide_index=True,
    )

    with st.expander("题材成交额口径核验（000001/399001 + 概念成交额）", expanded=False):
        try:
            st.write("题材面板 latest_dt：", str(theme_latest_dt.date()) if theme_latest_dt is not None else None)
            st.write("当前口径：", "盘中快照" if (effective_mode == "snapshot" and (snapshot_trade_dt is not None) and (theme_latest_dt == snapshot_trade_dt)) else "最近收盘")

            if rt_theme_msgs:
                st.write("快照/回退信息：")
                for _m in rt_theme_msgs:
                    st.write("-", _m)

            if rt_turnover_diag:
                raw_pair = {k: rt_turnover_diag.get(k) for k in [CODE_SH000001, CODE_SZ399001] if k in rt_turnover_diag}
                if raw_pair:
                    st.write("000001/399001 成交额快照原始值：", raw_pair)
                if "amount_scale_factor" in rt_turnover_diag:
                    st.write("指数成交额缩放因子：", rt_turnover_diag.get("amount_scale_factor"))
                if "scaled_amounts" in rt_turnover_diag:
                    st.write("000001/399001 缩放到元：", rt_turnover_diag.get("scaled_amounts"))

            # 当前用于题材面板分母的市场成交额（元）
            try:
                mv = market_turnover_for_theme.loc[theme_latest_dt] if theme_latest_dt in market_turnover_for_theme.index else np.nan
                st.write("market_turnover_for_theme_used(元)：", float(mv) if np.isfinite(mv) else None)
            except Exception:
                st.write("market_turnover_for_theme_used(元)：", None)
        except Exception as e:
            st.write("题材口径核验失败：", e)

    if etf_df is not None and not etf_df.empty:
        st.caption(
            f"ETF份额本地最新日期：{etf_df['trade_date'].max().date()}。"
            f"若深市历史不完整，常见原因是数据源不提供历史，只能从开始运行后每日累积快照。"
        )


if __name__ == "__main__":
    main()
