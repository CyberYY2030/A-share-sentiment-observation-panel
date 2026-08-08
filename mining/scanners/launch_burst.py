from __future__ import annotations

import math
import sqlite3
from collections import Counter
from typing import Any

import pandas as pd

from ..db import list_stock_trade_dates
from ..capabilities import SCREENING_DEFINITION_VERSION
from ..event_activity import EventActivityResult, build_event_activity
from ..selection_context import SelectionContext, build_selection_context
from ..trend_factors import cross_section_percentile
from .trend_embryo import build_v2_path_context
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


def evaluate_compression_launch(
    context: SelectionContext,
    *,
    params: dict[str, Any] | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Formal B/compression path using clean shared bars and T-1-only activity baselines."""
    p = {
        "ma_cluster_max": 0.07,
        "sigma_window": 20,
        "sigma_floor": 0.008,
        "first_lookback": 10,
        "first_sigma": 2.5,
        "launch_min_pct": 0.045,
        "launch_sigma": 3.0,
        "close_strength": 0.95,
        "vol_contract": 1.0,
        "activity_expand": 1.5,
        "amount_min": 200_000_000,
        **(params or {}),
    }
    diagnostics: dict[str, Any] = {"event_subtype": "compression_launch", "skipped_reason_counts": {}}
    if context.data_status != "ready" or context.bars.empty:
        diagnostics["skipped_reason_counts"] = {"data_unavailable": len(context.universe)}
        return pd.DataFrame(), diagnostics
    dates = [str(date) for date in context.diagnostics.get("clean_dates", [])]
    if str(context.trade_date) not in dates:
        dates.append(str(context.trade_date))
    if len(dates) < 46:
        diagnostics["skipped_reason_counts"] = {"insufficient_clean_history": len(context.universe)}
        return pd.DataFrame(), diagnostics
    activity: EventActivityResult = build_event_activity(context)
    activity_by_code = activity.rows.set_index("sec_code") if not activity.rows.empty else pd.DataFrame()
    paths = build_v2_path_context(context).set_index("sec_code")
    names = (
        context.universe.assign(sec_code=context.universe["sec_code"].astype(str).str.zfill(6))
        .set_index("sec_code")["sec_name"]
        .astype(str)
        .to_dict()
        if "sec_name" in context.universe.columns
        else {}
    )
    skipped = Counter(activity.skipped_reason_counts)
    rows: list[dict[str, Any]] = []
    for code, frame in context.bars.groupby(context.bars["sec_code"].astype(str).str.zfill(6), sort=True):
        if activity_by_code.empty or code not in activity_by_code.index:
            continue
        indexed = frame.assign(trade_date=frame["trade_date"].astype(str)).set_index("trade_date").reindex(dates)
        close = pd.to_numeric(indexed.get("adj_close"), errors="coerce")
        high = pd.to_numeric(indexed.get("adj_high", indexed.get("high")), errors="coerce")
        open_price = pd.to_numeric(indexed.get("adj_open", indexed.get("open")), errors="coerce")
        if not all(series.notna().iloc[-46:].all() for series in (close, high, open_price)):
            skipped["price_window_missing"] += 1
            continue
        source = str(activity_by_code.at[code, "activity_source"])
        activity_series = pd.to_numeric(indexed.get(source), errors="coerce")
        if not activity_series.iloc[-46:].notna().all():
            skipped["activity_window_missing"] += 1
            continue
        returns = close.pct_change(fill_method=None)
        sigma = float(returns.iloc[-21:-1].std())
        if not math.isfinite(sigma):
            skipped["sigma_window_missing"] += 1
            continue
        sigma = max(sigma, float(p["sigma_floor"]))
        ma5 = close.rolling(5, min_periods=5).mean()
        ma10 = close.rolling(10, min_periods=10).mean()
        ma20 = close.rolling(20, min_periods=20).mean()
        previous_mas = [float(series.iloc[-2]) for series in (ma5, ma10, ma20)]
        previous_cluster_width = (max(previous_mas) - min(previous_mas)) / min(previous_mas)
        previous_cluster_high = max(previous_mas)
        current_mas = [float(series.iloc[-1]) for series in (ma5, ma10, ma20)]
        current_cluster_high = max(current_mas)
        pct_t = float(returns.iloc[-1])
        current_close = float(close.iloc[-1])
        current_high = float(high.iloc[-1])
        current_open = float(open_price.iloc[-1])
        recent_pct = returns.iloc[-1 - int(p["first_lookback"]) : -1]
        recent_activity = activity_series.iloc[-6:-1]
        base_activity = activity_series.iloc[-46:-6]
        if recent_pct.isna().any() or recent_activity.isna().any() or base_activity.isna().any():
            skipped["required_window_missing"] += 1
            continue
        contract_ratio = float(recent_activity.mean() / base_activity.mean()) if float(base_activity.mean()) > 0 else math.nan
        expansion_ratio = float(activity_series.iloc[-1] / recent_activity.mean()) if float(recent_activity.mean()) > 0 else math.nan
        close_strength = current_close / current_high if current_high > 0 else math.nan
        threshold = max(float(p["launch_min_pct"]), float(p["launch_sigma"]) * sigma)
        qualifies = (
            previous_cluster_width <= float(p["ma_cluster_max"])
            and float(recent_pct.max()) < float(p["first_sigma"]) * sigma
            and contract_ratio <= float(p["vol_contract"])
            and pct_t >= threshold
            and current_close > current_open
            and close_strength >= float(p["close_strength"])
            and expansion_ratio >= float(p["activity_expand"])
            and current_open <= previous_cluster_high
            and current_close > current_cluster_high
        )
        if not qualifies:
            skipped["compression_gate_failed"] += 1
            continue
        amount = pd.to_numeric(indexed.get("amount"), errors="coerce").iloc[-1] if "amount" in indexed.columns else math.nan
        path = paths.loc[code] if not paths.empty and code in paths.index else None
        rows.append(
            {
                "sec_code": code,
                "sec_name": names.get(code, code),
                "event_subtype": "compression_launch",
                "reference_price": current_close,
                "sigma_multiple": pct_t / sigma,
                "cluster_width": previous_cluster_width,
                "volume_contract_ratio": contract_ratio,
                "activity_expand_ratio": expansion_ratio,
                "activity_source": source,
                "activity_pct": float(activity_by_code.at[code, "activity_pct"]),
                "close_strength": close_strength,
                "amount": float(amount) if pd.notna(amount) else None,
                "amount_min_diagnostic": bool(pd.notna(amount) and float(amount) >= float(p["amount_min"])),
                "path_context": path.path_context if path is not None else "常态",
                "limit_up_count_5d": int(path.limit_up_count_5d) if path is not None else 0,
                "small_yang_count": int(path.small_yang_count) if path is not None else 0,
                "single_day_max_change": float(path.single_day_max_change) if path is not None else math.nan,
            }
        )
    result = pd.DataFrame(rows)
    if not result.empty:
        result["sigma_pct"] = cross_section_percentile(result["sigma_multiple"])
        result["score"] = 0.65 * result["sigma_pct"] + 0.35 * result["activity_pct"]
        result = result.sort_values(["score", "activity_pct", "sec_code"], ascending=[False, False, True]).reset_index(drop=True)
    diagnostics["result_count"] = len(result)
    diagnostics["skipped_reason_counts"] = dict(sorted((key, value) for key, value in skipped.items() if value))
    return result, diagnostics


def select_compression_launch_from_context(context: SelectionContext) -> list[Candidate]:
    rows, _ = evaluate_compression_launch(context)
    candidates: list[Candidate] = []
    for rank, row in enumerate(rows.itertuples(index=False), start=1):
        candidates.append(
            Candidate(
                strategy_id=CompressionLaunchScanner.strategy_id,
                version=CompressionLaunchScanner.version,
                trade_date=context.trade_date,
                sec_type="stock",
                sec_code=row.sec_code,
                sec_name=row.sec_name,
                entry_price=float(row.reference_price),
                features={key: getattr(row, key) for key in rows.columns if key not in {"sec_code", "sec_name", "reference_price"}},
                rank=rank,
            )
        )
    return candidates


@register
class CompressionLaunchScanner(Scanner):
    strategy_id = "compression_launch"
    version = SCREENING_DEFINITION_VERSION
    kind = "stock"
    description = "V2 compression-launch event path using shared clean context and activity."
    default_params: dict[str, Any] = {}

    def run(self, conn: sqlite3.Connection, trade_date: str) -> list[Candidate]:
        context = build_selection_context(conn, trade_date, mode="close_final")
        self.last_universe_size = len(context.universe)
        return select_compression_launch_from_context(context)
