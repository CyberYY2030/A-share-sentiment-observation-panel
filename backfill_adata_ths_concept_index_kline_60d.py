# -*- coding: utf-8 -*-
"""backfill_adata_ths_concept_index_kline_60d_v3.py

目标：仅用 adata 拉取 **同花顺概念指数(88xxxx)** 的日K线（过去 N 个交易日左右），
并保存到本地 SQLite（ths_concept.db），供面板指标计算使用。

本版本修复了两个会导致“概念行情完全不更新”的问题：
1) 过滤文件默认应为 filtered_concept.csv，但旧脚本默认读 filtered_concept.xlsx，
   且读取失败会返回空集合；
2) 更关键：旧脚本把“LIMIT / upsert master / 回填K线”的主逻辑缩进到了
   `if exclude_set:` 代码块里——当 exclude_set 为空时，主逻辑根本不执行。

另外增强：
- --asof 支持 YYYYMMDD / YYYY-MM-DD / YYYY/MM/DD 等（数据库 trade_date 仍写入 YYYY/MM/DD）
- filtered_concept 支持 .csv 或 .xlsx（自动探测）
- purge_excluded 支持分批删除，避免 SQLite 变量上限
- 拉到的数据会按 [start,end] 过滤后再写入，降低写入量
- K线写入分批 commit，减少数据库开销

用法示例：
    python backfill_adata_ths_concept_index_kline_60d_v3.py --db ths_concept.db --days 60 --asof 2026-01-29
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import sqlite3
from typing import Optional, Tuple

import numpy as np
import pandas as pd


# ----------------------------
# Helpers
# ----------------------------
def _log(msg: str) -> None:
    ts = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[THS-KLINE {ts}] {msg}")


def _date(s: str) -> dt.date:
    """
    Accept:
      - 20260105
      - 2026-01-05
      - 2026/01/05
      - 2026.01.05
    """
    raw = str(s).strip()
    digits = re.sub(r"\D", "", raw)
    if len(digits) < 8:
        raise ValueError(f"bad date: {s}")
    y, m, d = digits[:4], digits[4:6], digits[6:8]
    return dt.date(int(y), int(m), int(d))


def _ymd(d: dt.date) -> str:
    return d.strftime("%Y%m%d")


def canonical_code(x) -> str:
    """
    Normalize a concept/index code into 6-digit string.
    Example: 880811, '880811.0' -> '880811'
    """
    s = str(x).strip()
    digits = re.sub(r"\D", "", s)
    if not digits:
        return ""
    if len(digits) < 6:
        digits = digits.zfill(6)
    if len(digits) > 6:
        digits = digits[:6]
    return digits


def detect_filtered_file(script_dir: str, filename: Optional[str] = None) -> Optional[str]:
    if filename:
        fp = filename if os.path.isabs(filename) else os.path.join(script_dir, filename)
        if os.path.exists(fp):
            return fp

    for cand in ("filtered_concept.csv", "filtered_concept.xlsx", "filtered_concept.xls"):
        fp = os.path.join(script_dir, cand)
        if os.path.exists(fp):
            return fp
    return None


def load_filtered_concepts(filename: Optional[str] = None) -> Tuple[set[str], Optional[str]]:
    """
    Read excluded concept index_code from filtered_concept.csv / .xlsx in the same folder as this script.
    Returns: (exclude_set, file_path_used)
    """
    script_dir = os.path.dirname(os.path.abspath(__file__))
    fp = detect_filtered_file(script_dir, filename)
    if not fp:
        return set(), None

    try:
        if fp.lower().endswith(".csv"):
            df = pd.read_csv(fp, dtype=str, encoding="utf-8", engine="python")
        else:
            df = pd.read_excel(fp, dtype=str)

        if df is None or df.empty:
            return set(), fp

        # try to find the code column
        col = None
        for c in df.columns:
            if str(c).strip().lower() in ("index_code", "concept_code", "code", "指数代码"):
                col = c
                break
        if col is None:
            # fallback: first column
            col = df.columns[0]

        codes = df[col].astype(str).apply(canonical_code)
        out = {c for c in codes.tolist() if c and len(c) == 6}
        return out, fp
    except Exception as e:
        _log(f"WARN: read filtered concepts failed: {e} (file={fp})")
        return set(), fp


# ----------------------------
# SQLite schema & upsert
# ----------------------------
def ensure_tables(con: sqlite3.Connection) -> None:
    con.execute(
        """
    CREATE TABLE IF NOT EXISTS concept_master (
      index_code TEXT PRIMARY KEY,
      name TEXT,
      concept_code TEXT,
      source TEXT,
      updated_at TEXT
    )
    """
    )
    con.execute(
        """
    CREATE TABLE IF NOT EXISTS concept_kline (
      trade_date TEXT NOT NULL,
      concept_code TEXT NOT NULL,
      close REAL,
      pre_close REAL,
      change_pct REAL,
      volume REAL,
      amount REAL,
      PRIMARY KEY(trade_date, concept_code)
    )
    """
    )
    con.execute("CREATE INDEX IF NOT EXISTS idx_concept_kline_code ON concept_kline(concept_code)")
    con.commit()


def upsert_df(con: sqlite3.Connection, table: str, df: pd.DataFrame, commit: bool = True) -> int:
    if df is None or df.empty:
        return 0
    d = df.copy()

    if "trade_date" in d.columns:
        # DB trade_date uses YYYY/MM/DD
        d["trade_date"] = pd.to_datetime(d["trade_date"]).dt.strftime("%Y/%m/%d")

    cols = list(d.columns)
    placeholders = ",".join(["?"] * len(cols))
    sql = f"INSERT OR REPLACE INTO {table} ({','.join(cols)}) VALUES ({placeholders})"
    con.executemany(sql, d[cols].itertuples(index=False, name=None))
    if commit:
        con.commit()
    return int(d.shape[0])


def _chunked(seq: list[str], size: int = 800):
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


# ----------------------------
# AData
# ----------------------------
def try_import_adata():
    try:
        import adata  # type: ignore

        return adata
    except Exception as e:
        _log(f"ERROR: cannot import adata: {e}")
        return None


def get_ths_concept_list(adata) -> pd.DataFrame:
    """Return df with at least index_code, name."""
    try:
        df = adata.stock.info.all_concept_code_ths()
        if isinstance(df, pd.DataFrame) and not df.empty:
            return df
    except Exception as e:
        _log(f"all_concept_code_ths failed: {e}")

    candidates = [
        ("adata.stock.info", "all_concept_ths"),
        ("adata.stock.info", "all_concept_code"),
    ]
    for mod_name, fn_name in candidates:
        try:
            mod = eval(mod_name)
            fn = getattr(mod, fn_name, None)
            if callable(fn):
                df = fn()
                if isinstance(df, pd.DataFrame) and not df.empty:
                    return df
        except Exception:
            pass

    raise RuntimeError("Cannot get THS concept list from adata in this environment.")


def fetch_concept_kline(adata, index_code: str, start: dt.date, end: dt.date) -> pd.DataFrame:
    mkt = adata.stock.market
    code = str(index_code)
    start_s, end_s = _ymd(start), _ymd(end)

    df = None

    fn = getattr(mkt, "get_market_concept_ths", None)
    if callable(fn):
        # try with date range if supported
        for kwargs in (
            {"index_code": code, "k_type": 1, "start_date": start_s, "end_date": end_s},
            {"concept_code": code, "k_type": 1, "start_date": start_s, "end_date": end_s},
            {"index_code": code, "k_type": 1},
            {"concept_code": code, "k_type": 1},
        ):
            try:
                df = fn(**kwargs)
                if isinstance(df, pd.DataFrame) and (not df.empty):
                    break
            except TypeError:
                continue
            except Exception:
                df = None

    if df is None or (isinstance(df, pd.DataFrame) and df.empty):
        fn2 = getattr(mkt, "get_market_index", None)
        if callable(fn2):
            for kwargs in (
                {"index_code": code, "k_type": 1, "start_date": start_s, "end_date": end_s},
                {"index_code": code, "k_type": 1},
            ):
                try:
                    df = fn2(**kwargs)
                    if isinstance(df, pd.DataFrame) and (not df.empty):
                        break
                except TypeError:
                    # some versions accept positional start/end
                    try:
                        df = fn2(code, start_s, end_s)
                        if isinstance(df, pd.DataFrame) and (not df.empty):
                            break
                    except Exception:
                        df = None
                except Exception:
                    df = None

    if df is None:
        return pd.DataFrame()

    if not isinstance(df, pd.DataFrame):
        try:
            df = pd.DataFrame(df)
        except Exception:
            return pd.DataFrame()

    if df.empty:
        return pd.DataFrame()

    df = df.copy()

    # trade_date
    if "trade_date" not in df.columns:
        for cand in ("date", "dt", "tradeTime", "trade_time"):
            if cand in df.columns:
                df["trade_date"] = df[cand]
                break
    if "trade_date" not in df.columns:
        return pd.DataFrame()

    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date

    # filter by [start,end] to reduce write volume
    df = df[(df["trade_date"] >= start) & (df["trade_date"] <= end)].copy()
    if df.empty:
        return pd.DataFrame()

    # code column
    if "concept_code" not in df.columns:
        if "index_code" in df.columns:
            df["concept_code"] = df["index_code"].astype(str)
        else:
            df["concept_code"] = code
    df["concept_code"] = df["concept_code"].astype(str).apply(canonical_code)

    # numeric
    for c in ("close", "volume", "amount", "change", "change_pct", "pre_close"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")

    if "pre_close" not in df.columns or df["pre_close"].isna().all():
        if "change" in df.columns and df["change"].notna().any():
            df["pre_close"] = df["close"] - df["change"]
        elif "change_pct" in df.columns and df["change_pct"].notna().any():
            df["pre_close"] = df["close"] / (1.0 + df["change_pct"] / 100.0)
        else:
            df["pre_close"] = np.nan

    out = pd.DataFrame(
        {
            "trade_date": df["trade_date"],
            "concept_code": df["concept_code"],
            "close": df["close"] if "close" in df.columns else np.nan,
            "pre_close": df["pre_close"],
            "change_pct": df["change_pct"] if "change_pct" in df.columns else np.nan,
            "volume": df["volume"] if "volume" in df.columns else np.nan,
            "amount": df["amount"] if "amount" in df.columns else np.nan,
        }
    )

    out = out.dropna(subset=["trade_date", "concept_code"]).drop_duplicates(subset=["trade_date", "concept_code"])
    out = out.sort_values(["concept_code", "trade_date"])

    # keep only last ~N trade days per concept to reduce db IO
    # (adata might still return a lot; tail() is per concept_code)
    out = out.groupby("concept_code", group_keys=False).tail(90).reset_index(drop=True)

    return out


# ----------------------------
# Main
# ----------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="ths_concept.db", help="sqlite db path")
    ap.add_argument("--days", type=int, default=60, help="backfill target trade days (approx window)")
    ap.add_argument("--asof", default="", help="YYYYMMDD / YYYY-MM-DD / YYYY/MM/DD, default today")
    ap.add_argument("--limit", type=int, default=0, help="limit number of concept indices for testing")
    ap.add_argument("--purge_excluded", type=int, default=1, help="purge excluded concepts from DB (1=yes,0=no)")
    ap.add_argument("--exclude_file", default="", help="optional exclude file name (csv/xlsx), default auto-detect")
    args = ap.parse_args()

    adata = try_import_adata()
    if adata is None:
        raise SystemExit(1)

    asof = _date(args.asof) if args.asof else dt.date.today()
    # wide calendar window; will be filtered by [start,end] later
    start = asof - dt.timedelta(days=max(90, args.days * 2))
    end = asof
    _log(f"asof={asof} start={start} end={end} db={args.db}")

    os.makedirs(os.path.dirname(os.path.abspath(args.db)) or ".", exist_ok=True)
    con = sqlite3.connect(args.db)
    ensure_tables(con)

    # concept list
    concepts = get_ths_concept_list(adata)

    # normalize col names
    if "index_code" not in concepts.columns:
        for cand in ("code", "idx_code", "indexCode"):
            if cand in concepts.columns:
                concepts["index_code"] = concepts[cand]
                break
    if "name" not in concepts.columns:
        for cand in ("concept_name", "index_name", "title"):
            if cand in concepts.columns:
                concepts["name"] = concepts[cand]
                break

    if "index_code" not in concepts.columns:
        raise RuntimeError("concept list has no index_code column (cannot proceed).")

    concepts = concepts.copy()
    concepts["index_code"] = concepts["index_code"].astype(str).apply(canonical_code)
    # filter 88xxxx only
    concepts = concepts[concepts["index_code"].str.match(r"^88\d{4}$")].copy()
    concepts = concepts.drop_duplicates(subset=["index_code"])
    _log(f"concept list rows={len(concepts)} (filtered 88xxxx)")

    # exclude concepts listed in filtered_concept.csv / xlsx
    exclude_set, exclude_fp = load_filtered_concepts(args.exclude_file if args.exclude_file else None)
    if exclude_set:
        before = len(concepts)
        concepts = concepts[~concepts["index_code"].astype(str).isin(exclude_set)].copy()
        _log(f"excluded {before - len(concepts)} concepts from {os.path.basename(exclude_fp) if exclude_fp else 'exclude_file'}; remain={len(concepts)}")
        if args.purge_excluded:
            try:
                ex_list = sorted(list(exclude_set))
                for chunk in _chunked(ex_list, 800):
                    q = ",".join(["?"] * len(chunk))
                    con.execute(f"DELETE FROM concept_master WHERE index_code IN ({q})", tuple(chunk))
                    con.execute(f"DELETE FROM concept_kline WHERE concept_code IN ({q})", tuple(chunk))
                con.commit()
                _log(f"purged excluded concepts from DB: {len(exclude_set)}")
            except Exception as e:
                _log(f"WARN: purge excluded failed: {e}")
    else:
        _log(f"exclude_set empty (file={exclude_fp or 'not found'}) -> will NOT exclude anything")

    # LIMIT should work regardless exclude_set is empty
    if args.limit and args.limit > 0:
        concepts = concepts.head(args.limit).copy()
        _log(f"LIMIT enabled -> {len(concepts)}")

    # upsert master
    master = pd.DataFrame(
        {
            "index_code": concepts["index_code"],
            "name": concepts["name"] if "name" in concepts.columns else "",
            "concept_code": concepts["concept_code"] if "concept_code" in concepts.columns else "",
            "source": concepts["source"] if "source" in concepts.columns else "",
            "updated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
    )
    upsert_df(con, "concept_master", master, commit=True)
    _log("concept_master upserted")

    total_rows = 0
    fail = 0
    for i, row in enumerate(concepts.itertuples(index=False), start=1):
        idx = getattr(row, "index_code")
        nm = getattr(row, "name", "")
        try:
            dfk = fetch_concept_kline(adata, idx, start, end)
            if dfk is None or dfk.empty:
                fail += 1
                _log(f"[FAIL] {idx} {nm}: empty df")
            else:
                total_rows += upsert_df(con, "concept_kline", dfk, commit=False)
        except Exception as e:
            fail += 1
            _log(f"[FAIL] {idx} {nm}: {e}")

        if i % 20 == 0:
            con.commit()
            _log(f"progress {i}/{len(concepts)} saved_rows={total_rows} fail={fail}")

    con.commit()
    con.close()
    _log(f"DONE saved_rows={total_rows} fail={fail} db={args.db}")


if __name__ == "__main__":
    main()
