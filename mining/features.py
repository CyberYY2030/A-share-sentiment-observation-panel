from __future__ import annotations

import math
from typing import Any

import pandas as pd


def _to_float(value: Any) -> float:
    try:
        if value is None:
            return math.nan
        return float(value)
    except (TypeError, ValueError):
        return math.nan


def change_pct(row: Any) -> float:
    close = _to_float(row["close"])
    pre_close = _to_float(row["pre_close"])
    if not math.isfinite(close) or not math.isfinite(pre_close) or pre_close == 0:
        return math.nan
    return (close - pre_close) / pre_close * 100.0


def upper_shadow_pct(row: Any) -> float:
    high = _to_float(row["high"])
    open_price = _to_float(row["open"])
    close = _to_float(row["close"])
    base = max(open_price, close)
    if not math.isfinite(high) or not math.isfinite(base) or base <= 0:
        return math.nan
    return (high - base) / base * 100.0


def high_breakout_from_open_pct(row: Any) -> float:
    high = _to_float(row["high"])
    open_price = _to_float(row["open"])
    if not math.isfinite(high) or not math.isfinite(open_price) or open_price <= 0:
        return math.nan
    return (high - open_price) / open_price * 100.0


def high_breakout_from_close_pct(row: Any) -> float:
    high = _to_float(row["high"])
    close = _to_float(row["close"])
    if not math.isfinite(high) or not math.isfinite(close) or close <= 0:
        return math.nan
    return (high - close) / close * 100.0


def lower_shadow_pct(row: Any) -> float:
    low = _to_float(row["low"])
    open_price = _to_float(row["open"])
    close = _to_float(row["close"])
    base = min(open_price, close)
    if not math.isfinite(low) or not math.isfinite(base) or base <= 0:
        return math.nan
    return (base - low) / base * 100.0


def limit_up_price(pre_close: float, board: str) -> float:
    rate = 1.2 if board in {"gem", "star"} else 1.1
    return round(float(pre_close) * rate, 2)


def is_limit_up(close: float, pre_close: float, board: str, tol: float = 0.01) -> bool:
    close_value = _to_float(close)
    pre_close_value = _to_float(pre_close)
    if (
        not math.isfinite(close_value)
        or not math.isfinite(pre_close_value)
        or close_value <= 0
        or pre_close_value <= 0
    ):
        return False
    return close_value >= limit_up_price(pre_close_value, board) - tol


def rolling_new_high(series: pd.Series, window: int) -> bool:
    values = pd.Series(series, dtype="float64").dropna()
    if window <= 0 or values.empty:
        return False
    window_values = values.tail(window)
    latest = float(window_values.iloc[-1])
    return math.isfinite(latest) and latest >= float(window_values.max())


def moving_average(series: pd.Series, window: int) -> pd.Series:
    values = pd.Series(series, dtype="float64")
    if window <= 0:
        return pd.Series(math.nan, index=values.index, dtype="float64")
    return values.rolling(window=window, min_periods=window).mean()


def volume_shrink_ratio(recent_volume: pd.Series, run_volume: pd.Series) -> float:
    recent = pd.to_numeric(pd.Series(recent_volume, dtype="float64"), errors="coerce").dropna()
    run = pd.to_numeric(pd.Series(run_volume, dtype="float64"), errors="coerce").dropna()
    if recent.empty or run.empty:
        return math.nan
    run_avg = float(run.mean())
    recent_avg = float(recent.mean())
    if not math.isfinite(run_avg) or run_avg <= 0 or not math.isfinite(recent_avg):
        return math.nan
    return recent_avg / run_avg

def ret_n(series_close: pd.Series, n: int) -> float:
    series = pd.Series(series_close, dtype="float64").dropna()
    if len(series) <= n:
        return math.nan
    start = float(series.iloc[-1 - n])
    end = float(series.iloc[-1])
    if not math.isfinite(start) or start == 0 or not math.isfinite(end):
        return math.nan
    return (end / start - 1.0) * 100.0


def rps(returns: pd.Series) -> pd.Series:
    series = pd.Series(returns, dtype="float64")
    if series.empty:
        return series
    ranks = series.rank(method="average", pct=True) * 100.0
    ranks[series.isna()] = math.nan
    return ranks


def board_kind(sec_code: str) -> str:
    code = str(sec_code or "")
    if code.startswith(("300", "301")):
        return "gem"
    if code.startswith(("688", "689")):
        return "star"
    return "main"
