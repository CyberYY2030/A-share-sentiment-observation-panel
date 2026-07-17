from __future__ import annotations

import math
import sqlite3
from typing import Any

import pandas as pd

from ..db import list_stock_trade_dates
from . import Candidate, Scanner, register


def _safe_float(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return math.nan
    return result if math.isfinite(result) else math.nan


def _cluster_days(widths: pd.Series, end_pos: int, threshold: float) -> int:
    count = 0
    for value in widths.iloc[: end_pos + 1].iloc[::-1]:
        width = _safe_float(value)
        if not math.isfinite(width) or width > threshold:
            break
        count += 1
    return count


def _candidate_row(frame: pd.DataFrame, trade_date: str, params: dict[str, Any]) -> dict[str, Any] | None:
    frame = frame.sort_values("trade_date").drop_duplicates("trade_date", keep="last").reset_index(drop=True)
    target_positions = frame.index[frame["trade_date"].astype(str).eq(trade_date)]
    if target_positions.empty:
        return None
    target_pos = int(target_positions[-1])

    numeric_columns = ("open", "high", "low", "close", "pre_close", "volume", "amount")
    for column in numeric_columns:
        if column not in frame.columns:
            frame[column] = math.nan
        frame[column] = pd.to_numeric(frame[column], errors="coerce")

    ma_windows = tuple(int(value) for value in params["ma_windows"])
    required_history = max(
        max(ma_windows),
        int(params["sigma_window"]) + 1,
        int(params["first_lookback"]),
        int(params["volume_base_n"]),
    )
    if target_pos < required_history:
        return None

    previous_close = frame["close"].shift(1)
    effective_pre_close = frame["pre_close"].where(frame["pre_close"] > 0, previous_close)
    frame["pct"] = frame["close"] / effective_pre_close - 1.0
    for window in ma_windows:
        frame[f"ma{window}"] = frame["close"].rolling(window, min_periods=window).mean()

    ma_columns = [f"ma{window}" for window in ma_windows]
    ma_min = frame[ma_columns].min(axis=1)
    ma_max = frame[ma_columns].max(axis=1)
    cluster_width = ((ma_max - ma_min) / ma_min).where(frame[ma_columns].notna().all(axis=1))
    sigma20 = (
        frame["pct"]
        .rolling(int(params["sigma_window"]), min_periods=int(params["sigma_window"]))
        .std()
        .shift(1)
        .clip(lower=float(params["sigma_floor"]))
    )

    row = frame.iloc[target_pos]
    pct_t = _safe_float(row["pct"])
    sigma_t = _safe_float(sigma20.iloc[target_pos])
    previous_cluster_width = _safe_float(cluster_width.iloc[target_pos - 1])
    previous_cluster_high = _safe_float(ma_max.iloc[target_pos - 1])
    current_cluster_high = _safe_float(ma_max.iloc[target_pos])
    open_t = _safe_float(row["open"])
    high_t = _safe_float(row["high"])
    close_t = _safe_float(row["close"])
    volume_t = _safe_float(row["volume"])
    amount_t = _safe_float(row["amount"])
    if not all(
        math.isfinite(value)
        for value in (
            pct_t,
            sigma_t,
            previous_cluster_width,
            previous_cluster_high,
            current_cluster_high,
            open_t,
            high_t,
            close_t,
            volume_t,
            amount_t,
        )
    ):
        return None
    if high_t <= 0 or close_t <= 0 or volume_t <= 0:
        return None

    first_lookback = int(params["first_lookback"])
    recent_pct = pd.to_numeric(
        frame.iloc[target_pos - first_lookback : target_pos]["pct"], errors="coerce"
    ).dropna()
    volume_recent_n = int(params["volume_recent_n"])
    volume_base_n = int(params["volume_base_n"])
    recent_volume = pd.to_numeric(
        frame.iloc[target_pos - volume_recent_n : target_pos]["volume"], errors="coerce"
    ).dropna()
    base_volume = pd.to_numeric(
        frame.iloc[target_pos - volume_base_n : target_pos]["volume"], errors="coerce"
    ).dropna()
    if len(recent_pct) < first_lookback or len(recent_volume) < volume_recent_n or len(base_volume) < volume_base_n:
        return None

    recent_volume_mean = float(recent_volume.mean())
    base_volume_mean = float(base_volume.mean())
    if recent_volume_mean <= 0 or base_volume_mean <= 0:
        return None
    volume_ratio = volume_t / recent_volume_mean
    volume_contract_ratio = recent_volume_mean / base_volume_mean
    first_max_pct = float(recent_pct.max())
    close_strength = close_t / high_t
    launch_threshold = max(float(params["launch_min_pct"]), float(params["launch_sigma"]) * sigma_t)

    qualifies = (
        previous_cluster_width <= float(params["ma_cluster_max"])
        and first_max_pct < float(params["first_sigma"]) * sigma_t
        and volume_contract_ratio <= float(params["vol_contract"])
        and pct_t >= launch_threshold
        and close_t > open_t
        and close_strength >= float(params["close_strength"])
        and volume_ratio >= float(params["vol_expand"])
        and open_t <= previous_cluster_high
        and close_t > current_cluster_high
        and amount_t >= float(params["amount_min"])
        and close_t < float(params["price_max"])
    )
    if not qualifies:
        return None

    sigma_multiple = pct_t / sigma_t
    return {
        "sec_code": str(row["sec_code"]).zfill(6),
        "sec_name": str(row.get("sec_name") or row["sec_code"]),
        "entry_price": close_t,
        "pct": pct_t,
        "volume_ratio": volume_ratio,
        "sigma20": sigma_t,
        "sigma_multiple": sigma_multiple,
        "cluster_width": previous_cluster_width,
        "cluster_days": _cluster_days(cluster_width, target_pos - 1, float(params["ma_cluster_max"])),
        "volume_contract_ratio": volume_contract_ratio,
        "first_max_pct": first_max_pct,
        "close_strength": close_strength,
        "amount": amount_t,
        "score": sigma_multiple * volume_ratio,
    }


def select_candidates_from_history(
    history: pd.DataFrame,
    trade_date: str,
    params: dict[str, Any],
    strategy_id: str,
    version: str,
) -> list[Candidate]:
    if history.empty or "sec_code" not in history.columns or "trade_date" not in history.columns:
        return []

    frame = history.copy()
    if "sec_type" in frame.columns:
        frame = frame[frame["sec_type"].astype(str).eq("stock")]
    if frame.empty:
        return []
    frame["sec_code"] = frame["sec_code"].astype(str).str.zfill(6)
    if "sec_name" not in frame.columns:
        frame["sec_name"] = frame["sec_code"]

    frame = frame.sort_values(["sec_code", "trade_date"]).reset_index(drop=True)
    for column in ("open", "high", "close", "pre_close", "amount"):
        if column not in frame.columns:
            frame[column] = math.nan
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    previous_close = frame.groupby("sec_code", sort=False)["close"].shift(1)
    effective_pre_close = frame["pre_close"].where(frame["pre_close"] > 0, previous_close)
    target = frame["trade_date"].astype(str).eq(trade_date)
    cheap_gate = (
        target
        & (frame["close"] / effective_pre_close - 1.0 >= float(params["launch_min_pct"]))
        & frame["close"].gt(frame["open"])
        & (frame["close"] / frame["high"] >= float(params["close_strength"]))
        & frame["amount"].ge(float(params["amount_min"]))
        & frame["close"].lt(float(params["price_max"]))
    )
    eligible_codes = set(frame.loc[cheap_gate, "sec_code"])
    if not eligible_codes:
        return []
    frame = frame[frame["sec_code"].isin(eligible_codes)]

    rows: list[dict[str, Any]] = []
    for _, stock_frame in frame.groupby("sec_code", sort=False):
        candidate = _candidate_row(stock_frame, trade_date, params)
        if candidate is not None:
            rows.append(candidate)
    if not rows:
        return []

    selected = (
        pd.DataFrame(rows)
        .sort_values(
            ["sigma_multiple", "cluster_days", "sec_code"],
            ascending=[True, False, True],
        )
        .head(int(params["daily_cap"]))
    )
    candidates: list[Candidate] = []
    for rank, row in enumerate(selected.itertuples(index=False), start=1):
        features = {
            "pct": float(row.pct),
            "volume_ratio": float(row.volume_ratio),
            "sigma20": float(row.sigma20),
            "sigma_multiple": float(row.sigma_multiple),
            "cluster_width": float(row.cluster_width),
            "cluster_days": int(row.cluster_days),
            "volume_contract_ratio": float(row.volume_contract_ratio),
            "first_max_pct": float(row.first_max_pct),
            "close_strength": float(row.close_strength),
            "amount": float(row.amount),
            "score": float(row.score),
        }
        candidates.append(
            Candidate(
                strategy_id=strategy_id,
                version=version,
                trade_date=trade_date,
                sec_type="stock",
                sec_code=row.sec_code,
                sec_name=row.sec_name,
                entry_price=float(row.entry_price),
                features=features,
                rank=rank,
            )
        )
    return candidates


def _load_history(conn: sqlite3.Connection, dates: list[str]) -> pd.DataFrame:
    if not dates:
        return pd.DataFrame()
    date_ph = ",".join("?" for _ in dates)
    history = pd.read_sql_query(
        f"""
        SELECT sec_type, sec_code, trade_date, open, high, low, close,
               pre_close, volume, amount
        FROM ash.kline_daily
        WHERE sec_type='stock'
          AND trade_date IN ({date_ph})
        ORDER BY sec_code, trade_date
        """,
        conn,
        params=dates,
    )
    if history.empty:
        return history
    try:
        names = pd.read_sql_query("SELECT sec_code, name AS sec_name FROM ash.stock_info", conn)
    except sqlite3.DatabaseError:
        names = pd.DataFrame(columns=["sec_code", "sec_name"])
    if names.empty:
        history["sec_name"] = history["sec_code"]
    else:
        names["sec_code"] = names["sec_code"].astype(str).str.zfill(6)
        history["sec_code"] = history["sec_code"].astype(str).str.zfill(6)
        history = history.merge(names.drop_duplicates("sec_code", keep="last"), on="sec_code", how="left")
        history["sec_name"] = history["sec_name"].fillna(history["sec_code"])
    return history


@register
class LaunchBurstScanner(Scanner):
    strategy_id = "launch_burst"
    version = "v1.0"
    kind = "stock"
    description = "First high-volume launch bar after low-volatility moving-average compression."
    default_params = {
        "ma_windows": (5, 10, 20, 50),
        "ma_cluster_max": 0.07,
        "sigma_window": 20,
        "sigma_floor": 0.008,
        "first_lookback": 10,
        "first_sigma": 2.5,
        "volume_recent_n": 5,
        "volume_base_n": 40,
        "vol_contract": 1.0,
        "launch_min_pct": 0.045,
        "launch_sigma": 3.0,
        "close_strength": 0.95,
        "vol_expand": 1.5,
        "amount_min": 200_000_000,
        "price_max": 3_000.0,
        "daily_cap": 10,
        "history_days": 90,
    }

    def run(self, conn: sqlite3.Connection, trade_date: str) -> list[Candidate]:
        dates = list_stock_trade_dates(
            conn,
            end_date=trade_date,
            limit=int(self.params["history_days"]),
            include_end=True,
        )
        if trade_date not in dates:
            self.last_universe_size = 0
            return []
        history = _load_history(conn, dates)
        target_rows = history[history["trade_date"].astype(str).eq(trade_date)] if not history.empty else history
        self.last_universe_size = int(target_rows["sec_code"].nunique()) if not target_rows.empty else 0
        return select_candidates_from_history(
            history=history,
            trade_date=trade_date,
            params=self.params,
            strategy_id=self.strategy_id,
            version=self.version,
        )
