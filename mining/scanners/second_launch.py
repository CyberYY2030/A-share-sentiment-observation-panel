from __future__ import annotations

import math
import sqlite3
from typing import Any

import pandas as pd

from ..db import list_stock_trade_dates
from ..features import volume_shrink_ratio
from ..watchlist import build_watchlist
from . import Candidate, Scanner, register


def _load_stock_history(conn: sqlite3.Connection, sec_codes: list[str], start_date: str, end_date: str) -> pd.DataFrame:
    if not sec_codes:
        return pd.DataFrame()
    code_ph = ",".join("?" for _ in sec_codes)
    return pd.read_sql_query(
        f"""
        SELECT sec_code, trade_date, close, low, volume
        FROM ash.kline_daily
        WHERE sec_type='stock'
          AND sec_code IN ({code_ph})
          AND trade_date BETWEEN ? AND ?
        """,
        conn,
        params=[*sec_codes, start_date, end_date],
    )


def _calendar_gap(calendar_index: dict[str, int], start: str, end: str) -> int | None:
    start_idx = calendar_index.get(start)
    end_idx = calendar_index.get(end)
    if start_idx is None or end_idx is None:
        return None
    return int(end_idx - start_idx)


def _safe_float(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return math.nan
    return result if math.isfinite(result) else math.nan


def select_candidates_from_watchlist(
    conn: sqlite3.Connection,
    trade_date: str,
    watchlist: pd.DataFrame,
    params: dict[str, Any],
    strategy_id: str,
    version: str,
) -> list[Candidate]:
    if watchlist.empty:
        return []

    lookback = int(params["lookback"])
    vol_recent_n = int(params["vol_recent_n"])
    vol_run_n = int(params["vol_run_n"])
    stop_n = int(params["stop_n"])
    history_limit = max(lookback + vol_run_n + 20, 65)
    dates = list_stock_trade_dates(conn, end_date=trade_date, limit=history_limit, include_end=True)
    if trade_date not in dates or len(dates) < max(vol_recent_n, vol_run_n, stop_n) + 1:
        return []

    sec_codes = watchlist["sec_code"].astype(str).str.zfill(6).drop_duplicates().tolist()
    history = _load_stock_history(conn, sec_codes, dates[0], trade_date)
    if history.empty:
        return []
    history["sec_code"] = history["sec_code"].astype(str).str.zfill(6)
    close_pivot = history.pivot_table(index="trade_date", columns="sec_code", values="close", aggfunc="last").reindex(dates)
    low_pivot = history.pivot_table(index="trade_date", columns="sec_code", values="low", aggfunc="last").reindex(dates)
    volume_pivot = history.pivot_table(index="trade_date", columns="sec_code", values="volume", aggfunc="last").reindex(dates)
    calendar_index = {day: idx for idx, day in enumerate(dates)}

    rows: list[dict[str, Any]] = []
    for item in watchlist.itertuples(index=False):
        code = str(item.sec_code).zfill(6)
        if code not in close_pivot.columns or code not in volume_pivot.columns:
            continue
        close = _safe_float(getattr(item, "close", math.nan))
        pullback_pct = _safe_float(getattr(item, "pullback_pct", math.nan))
        ma_proximity = _safe_float(getattr(item, "ma_proximity", math.nan))
        ma10 = _safe_float(getattr(item, "ma10", math.nan))
        if not math.isfinite(close) or close <= 0:
            continue
        if not math.isfinite(pullback_pct) or pullback_pct > -float(params["pullback_min"]):
            continue
        if not math.isfinite(ma_proximity) or ma_proximity > float(params["ma_tol"]):
            continue

        recent_volume = pd.to_numeric(volume_pivot[code].dropna().tail(vol_recent_n), errors="coerce")
        if len(recent_volume) < vol_recent_n:
            continue
        first_flag_date = str(item.first_flag_date)
        first_idx = calendar_index.get(first_flag_date)
        if first_idx is None:
            continue
        run_start = max(0, first_idx - vol_run_n + 1)
        run_dates = dates[run_start : first_idx + 1]
        run_volume = pd.to_numeric(volume_pivot.loc[run_dates, code].dropna(), errors="coerce")
        if len(run_volume) < vol_run_n:
            continue
        vol_recent_avg = float(recent_volume.mean())
        vol_run_avg = float(run_volume.mean())
        shrink_value = volume_shrink_ratio(recent_volume, run_volume)
        if not math.isfinite(vol_recent_avg) or not math.isfinite(vol_run_avg) or not math.isfinite(shrink_value):
            continue
        if shrink_value > float(params["shrink_ratio"]):
            continue

        stop_dates = dates[-stop_n:]
        recent_lows = pd.to_numeric(low_pivot.loc[stop_dates, code].dropna(), errors="coerce")
        no_new_low = False
        if len(recent_lows) >= stop_n and len(recent_lows.iloc[:-1]) > 0:
            no_new_low = float(recent_lows.iloc[-1]) > float(recent_lows.iloc[:-1].min())
        close_above_ma10 = math.isfinite(ma10) and close >= ma10
        stop_signal = bool(no_new_low or close_above_ma10)
        if not stop_signal:
            continue

        ma_score = max(0.0, 1.0 - ma_proximity / max(float(params["ma_tol"]), 1e-9))
        shrink_score = max(0.0, 1.0 - shrink_value / max(float(params["shrink_ratio"]), 1e-9))
        score = float(params["w_stop"]) * float(stop_signal) + float(params["w_ma"]) * ma_score + float(params["w_shrink"]) * shrink_score
        rows.append(
            {
                "sec_code": code,
                "sec_name": item.sec_name,
                "entry_price": close,
                "pullback_pct": pullback_pct,
                "ma_proximity": ma_proximity,
                "ma10": ma10,
                "ma20": _safe_float(getattr(item, "ma20", math.nan)),
                "volume_shrink_ratio": shrink_value,
                "vol_recent_avg": vol_recent_avg,
                "vol_run_avg": vol_run_avg,
                "days_since_flag": getattr(item, "days_since_flag", None),
                "days_since_peak": getattr(item, "days_since_peak", None),
                "days_since_first_flag": _calendar_gap(calendar_index, first_flag_date, trade_date),
                "first_flag_date": first_flag_date,
                "last_flag_date": str(item.last_flag_date),
                "peak_date": str(item.peak_date),
                "peak_close_since_flag": _safe_float(getattr(item, "peak_close_since_flag", math.nan)),
                "flag_count": int(getattr(item, "flag_count", 0)),
                "flag_strategies": str(getattr(item, "flag_strategies", "")),
                "stop_signal": stop_signal,
                "no_new_low": bool(no_new_low),
                "close_above_ma10": bool(close_above_ma10),
                "score": score,
            }
        )

    if not rows:
        return []
    selected = (
        pd.DataFrame(rows)
        .sort_values(["score", "ma_proximity", "volume_shrink_ratio", "sec_code"], ascending=[False, True, True, True])
        .head(int(params["top_n"]))
    )
    candidates: list[Candidate] = []
    for rank, row in enumerate(selected.itertuples(index=False), start=1):
        candidates.append(
            Candidate(
                strategy_id=strategy_id,
                version=version,
                trade_date=trade_date,
                sec_type="stock",
                sec_code=row.sec_code,
                sec_name=row.sec_name,
                entry_price=float(row.entry_price),
                features={
                    "pullback_pct": float(row.pullback_pct),
                    "ma_proximity": float(row.ma_proximity),
                    "ma10": float(row.ma10) if math.isfinite(float(row.ma10)) else math.nan,
                    "ma20": float(row.ma20) if math.isfinite(float(row.ma20)) else math.nan,
                    "shrink_ratio": float(row.volume_shrink_ratio),
                    "volume_shrink_ratio": float(row.volume_shrink_ratio),
                    "vol_recent_avg": float(row.vol_recent_avg),
                    "vol_run_avg": float(row.vol_run_avg),
                    "days_since_flag": row.days_since_flag,
                    "days_since_peak": row.days_since_peak,
                    "days_since_first_flag": row.days_since_first_flag,
                    "first_flag_date": row.first_flag_date,
                    "last_flag_date": row.last_flag_date,
                    "peak_date": row.peak_date,
                    "peak_close_since_flag": float(row.peak_close_since_flag),
                    "flag_count": int(row.flag_count),
                    "flag_strategies": row.flag_strategies,
                    "stop_signal": bool(row.stop_signal),
                    "no_new_low": bool(row.no_new_low),
                    "close_above_ma10": bool(row.close_above_ma10),
                    "score": float(row.score),
                },
                rank=rank,
            )
        )
    return candidates


@register
class SecondLaunchScanner(Scanner):
    strategy_id = "second_launch"
    version = "v1.0"
    kind = "stock"
    description = "Watchlist-based second-launch pullback scanner for prior strength names."
    default_params = {
        "lookback": 40,
        "source_strategies": ("trend_embryo", "true_leader"),
        "pullback_min": 0.08,
        "ma_tol": 0.03,
        "shrink_ratio": 0.7,
        "vol_recent_n": 5,
        "vol_run_n": 5,
        "stop_n": 3,
        "top_n": 20,
        "w_stop": 1.0,
        "w_ma": 1.0,
        "w_shrink": 1.0,
    }

    def run(self, conn: sqlite3.Connection, trade_date: str) -> list[Candidate]:
        watchlist = build_watchlist(
            conn,
            trade_date,
            lookback=int(self.params["lookback"]),
            source_strategies=tuple(self.params["source_strategies"]),
        )
        self.last_universe_size = len(watchlist)
        return select_candidates_from_watchlist(
            conn=conn,
            trade_date=trade_date,
            watchlist=watchlist,
            params=self.params,
            strategy_id=self.strategy_id,
            version=self.version,
        )
