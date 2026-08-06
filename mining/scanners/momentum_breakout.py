from __future__ import annotations

import math
import sqlite3
from collections import Counter
from typing import Any

import pandas as pd

from ..db import list_stock_trade_dates
from ..event_activity import EventActivityResult, build_event_activity
from ..features import (
    high_breakout_from_close_pct,
    high_breakout_from_open_pct,
    upper_shadow_pct,
)
from ..selection_context import SelectionContext, build_selection_context
from ..trend_factors import cross_section_percentile
from ..universe import build_universe
from .trend_embryo import build_v2_path_context
from . import Candidate, Scanner, register


def _load_stock_history(
    conn: sqlite3.Connection, sec_codes: list[str], dates: list[str]
) -> pd.DataFrame:
    if not sec_codes or not dates:
        return pd.DataFrame()
    date_ph = ",".join("?" for _ in dates)
    code_ph = ",".join("?" for _ in sec_codes)
    sql = f"""
        SELECT sec_code, trade_date, close, change_pct
        FROM ash.kline_daily
        WHERE sec_type='stock'
          AND trade_date IN ({date_ph})
          AND sec_code IN ({code_ph})
    """
    return pd.read_sql_query(sql, conn, params=[*dates, *sec_codes])


def _load_rps_exclusion_codes(*_args: object, **_kwargs: object) -> set[str]:
    """Compatibility symbol only: v2 removes the momentum-to-RPS exclusion coupling."""
    raise RuntimeError("RPS exclusion coupling was removed from momentum breakout.")


def select_candidates_from_universe(
    conn: sqlite3.Connection,
    trade_date: str,
    universe: pd.DataFrame,
    params: dict[str, float],
    strategy_id: str,
    version: str,
    rps_exclusion_codes: set[str] | None = None,
) -> list[Candidate]:
    if universe.empty:
        return []

    previous_dates = list_stock_trade_dates(
        conn, end_date=trade_date, limit=6, include_end=False
    )
    if len(previous_dates) < 6:
        return []

    history = _load_stock_history(conn, universe["sec_code"].tolist(), previous_dates)
    if history.empty:
        return []

    closes = history[["sec_code", "trade_date", "close"]].copy()
    pivot = closes.pivot_table(
        index="trade_date", columns="sec_code", values="close", aggfunc="last"
    ).sort_index()
    ret_5d = ((pivot.iloc[-1] / pivot.iloc[0]) - 1.0) * 100.0
    ret_5d.name = "ret_5d"
    recent_window_gain_pct = ((pivot.iloc[-1] / pivot.iloc[-4]) - 1.0) * 100.0
    recent_window_gain_pct.name = "recent_window_gain_pct"
    recent_spike_dates = previous_dates[-3:]
    recent_spike = (
        history[history["trade_date"].isin(recent_spike_dates)]
        .groupby("sec_code", as_index=True)["change_pct"]
        .max()
        .rename("recent_spike_max_pct")
    )

    df = (
        universe.merge(ret_5d.reset_index(), on="sec_code", how="left")
        .merge(recent_window_gain_pct.reset_index(), on="sec_code", how="left")
        .merge(recent_spike.reset_index(), on="sec_code", how="left")
    )
    df["upper_shadow"] = df.apply(upper_shadow_pct, axis=1)
    df["high_over_open_pct"] = df.apply(high_breakout_from_open_pct, axis=1)
    df["high_over_close_pct"] = df.apply(high_breakout_from_close_pct, axis=1)
    df["amount"] = pd.to_numeric(df["amount"], errors="coerce")
    current_change_pct = pd.to_numeric(df["change_pct"], errors="coerce")
    latest_change_not_weak = current_change_pct > params["shadow_latest_change_pct_floor"]

    main_mask = df["board"] == "main"
    fast_mask = df["board"].isin(["gem", "star"])
    gain_mask = (
        main_mask
        & (current_change_pct > params["gain_main_lo"])
        & (current_change_pct < params["gain_main_hi"])
    ) | (
        fast_mask
        & (current_change_pct > params["gain_gem_lo"])
        & (current_change_pct < params["gain_gem_hi"])
    )
    shadow_mask = (
        main_mask
        & (df["high_over_open_pct"] > params["high_over_open_min"])
        & (df["high_over_close_pct"] > params["high_over_close_min"])
        & latest_change_not_weak
        & (current_change_pct < params["gain_main_hi"])
    ) | (
        fast_mask
        & (df["high_over_open_pct"] > params["high_over_open_min"])
        & (df["high_over_close_pct"] > params["high_over_close_min"])
        & latest_change_not_weak
        & (current_change_pct < params["gain_gem_hi"])
    )
    ret_mask = (df["ret_5d"] > params["ret_5d_lo"]) & (df["ret_5d"] < params["ret_5d_hi"])
    amount_mask = df["amount"] >= params["amount_min"]
    early_strength_adjustment_mask = (
        (pd.to_numeric(df["recent_spike_max_pct"], errors="coerce") >= params["early_strength_prior_gain_min"])
        & (pd.to_numeric(df["upper_shadow"], errors="coerce") > params["early_strength_upper_shadow_min"])
        & (current_change_pct < params["early_strength_latest_gain_max"])
        & (df["amount"] > params["early_strength_amount_min"])
    )
    early_strength_pool = df[early_strength_adjustment_mask].copy()
    if not early_strength_pool.empty:
        early_strength_pool = early_strength_pool.sort_values(
            by=["recent_window_gain_pct", "recent_spike_max_pct", "amount", "sec_code"],
            ascending=[False, False, False, True],
        ).head(int(params["early_strength_top_n"]))
    early_strength_codes = set(early_strength_pool["sec_code"].tolist())
    early_strength_adjustment_mask = df["sec_code"].isin(early_strength_codes)
    path_masks = {
        "gain": gain_mask & ret_mask & amount_mask,
        "shadow": shadow_mask & ret_mask & amount_mask,
        "early_strength_adjustment": early_strength_adjustment_mask,
    }

    selected = df[pd.concat(path_masks.values(), axis=1).any(axis=1)].copy()
    if selected.empty:
        return []

    selected["path"] = [
        "+".join(name for name, mask in path_masks.items() if bool(mask.loc[idx]))
        for idx in selected.index
    ]
    selected = selected.sort_values(
        by=["amount", "change_pct", "sec_code"], ascending=[False, False, True]
    ).reset_index(drop=True)

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
                entry_price=float(row.close),
                features={
                    "change_pct": float(row.change_pct),
                    "upper_shadow": float(row.upper_shadow),
                    "high_over_open_pct": float(row.high_over_open_pct),
                    "high_over_close_pct": float(row.high_over_close_pct),
                    "amount": float(row.amount),
                    "ret_5d": float(row.ret_5d),
                    "recent_spike_max_pct": float(row.recent_spike_max_pct),
                    "recent_window_gain_pct": float(row.recent_window_gain_pct),
                    "board": row.board,
                    "path": row.path,
                },
                rank=rank,
            )
        )
    return candidates


@register
class MomentumBreakoutScanner(Scanner):
    strategy_id = "momentum_breakout"
    version = "v1.0"
    kind = "stock"
    description = "Late-session momentum breakout scanner."
    default_params = {
        "amount_min": 1_000_000_000,
        "gain_main_lo": 6.0,
        "gain_main_hi": 9.5,
        "gain_gem_lo": 7.0,
        "gain_gem_hi": 13.0,
        "high_over_open_min": 3.0,
        "high_over_close_min": 5.0,
        "shadow_latest_change_pct_floor": -3.0,
        "ret_5d_lo": 5.0,
        "ret_5d_hi": 30.0,
        "early_strength_prior_gain_min": 8.0,
        "early_strength_upper_shadow_min": 4.0,
        "early_strength_latest_gain_max": 5.0,
        "early_strength_amount_min": 1_000_000_000,
        "early_strength_top_n": 10,
    }

    def run(self, conn: sqlite3.Connection, trade_date: str) -> list[Candidate]:
        universe = build_universe(conn, trade_date)
        self.last_universe_size = len(universe)
        return select_candidates_from_universe(
            conn=conn,
            trade_date=trade_date,
            universe=universe,
            params=self.params,
            strategy_id=self.strategy_id,
            version=self.version,
        )


def evaluate_momentum_anomaly(
    context: SelectionContext,
    *,
    params: dict[str, Any] | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Formal B/momentum path; A candidates are deliberately allowed to overlap."""
    p = {
        "gain_main_lo": 6.0,
        "gain_main_hi": 9.5,
        "gain_fast_lo": 7.0,
        "gain_fast_hi": 13.0,
        "high_over_open_min": 3.0,
        "high_over_close_min": 5.0,
        "shadow_latest_floor": -3.0,
        "ret_5d_lo": 5.0,
        "ret_5d_hi": 30.0,
        "prior_pulse_min": 8.0,
        "prior_shadow_min": 4.0,
        "latest_gain_max": 5.0,
        **(params or {}),
    }
    diagnostics: dict[str, Any] = {"event_subtype": "momentum_anomaly", "skipped_reason_counts": {}}
    if context.data_status != "ready" or context.bars.empty:
        diagnostics["skipped_reason_counts"] = {"data_unavailable": len(context.universe)}
        return pd.DataFrame(), diagnostics
    dates = [str(date) for date in context.diagnostics.get("clean_dates", [])]
    if str(context.trade_date) not in dates:
        dates.append(str(context.trade_date))
    if len(dates) < 6:
        diagnostics["skipped_reason_counts"] = {"insufficient_clean_history": len(context.universe)}
        return pd.DataFrame(), diagnostics
    activity: EventActivityResult = build_event_activity(context)
    activity_by_code = activity.rows.set_index("sec_code") if not activity.rows.empty else pd.DataFrame()
    paths = build_v2_path_context(context).set_index("sec_code")
    universe = context.universe.copy()
    universe["sec_code"] = universe["sec_code"].astype(str).str.zfill(6)
    board_by_code = universe.set_index("sec_code").get("board", pd.Series(dtype=object)).to_dict()
    name_by_code = universe.set_index("sec_code").get("sec_name", pd.Series(dtype=object)).astype(str).to_dict()
    skipped = Counter(activity.skipped_reason_counts)
    rows: list[dict[str, Any]] = []
    for code, frame in context.bars.groupby(context.bars["sec_code"].astype(str).str.zfill(6), sort=True):
        if activity_by_code.empty or code not in activity_by_code.index:
            continue
        indexed = frame.assign(trade_date=frame["trade_date"].astype(str)).set_index("trade_date").reindex(dates)
        close = pd.to_numeric(indexed.get("adj_close"), errors="coerce")
        raw_open = pd.to_numeric(indexed.get("open"), errors="coerce")
        raw_high = pd.to_numeric(indexed.get("high"), errors="coerce")
        raw_change = pd.to_numeric(indexed.get("change_pct"), errors="coerce")
        if not all(series.notna().iloc[-6:].all() for series in (close, raw_open, raw_high, raw_change)):
            skipped["price_window_missing"] += 1
            continue
        current_change = float(raw_change.iloc[-1])
        ret_5d = (float(close.iloc[-1]) / float(close.iloc[-6]) - 1.0) * 100.0
        recent_window_gain = (float(close.iloc[-1]) / float(close.iloc[-4]) - 1.0) * 100.0
        recent_spike = float(raw_change.iloc[-4:-1].max())
        high_over_open = (float(raw_high.iloc[-1]) / float(raw_open.iloc[-1]) - 1.0) * 100.0
        high_over_close = (float(raw_high.iloc[-1]) / float(close.iloc[-1]) - 1.0) * 100.0
        board = str(board_by_code.get(code) or "")
        fast_board = board in {"gem", "star"}
        gain = (
            float(p["gain_fast_lo"]) < current_change < float(p["gain_fast_hi"])
            if fast_board
            else float(p["gain_main_lo"]) < current_change < float(p["gain_main_hi"])
        )
        shadow = (
            high_over_open > float(p["high_over_open_min"])
            and high_over_close > float(p["high_over_close_min"])
            and current_change > float(p["shadow_latest_floor"])
        )
        early_pulse = (
            recent_spike >= float(p["prior_pulse_min"])
            and high_over_close > float(p["prior_shadow_min"])
            and current_change < float(p["latest_gain_max"])
        )
        in_path_range = float(p["ret_5d_lo"]) < ret_5d < float(p["ret_5d_hi"])
        if not ((gain and in_path_range) or (shadow and in_path_range) or early_pulse):
            skipped["momentum_gate_failed"] += 1
            continue
        path = paths.loc[code] if not paths.empty and code in paths.index else None
        rows.append(
            {
                "sec_code": code,
                "sec_name": name_by_code.get(code, code),
                "event_subtype": "momentum_anomaly",
                "reference_price": float(close.iloc[-1]),
                "board": board,
                "change_pct": current_change,
                "ret_5d": ret_5d,
                "recent_window_gain_pct": recent_window_gain,
                "recent_spike_max_pct": recent_spike,
                "high_over_open_pct": high_over_open,
                "high_over_close_pct": high_over_close,
                "event_path": "+".join(
                    name for name, matched in (("gain", gain), ("shadow", shadow), ("prior_pulse", early_pulse)) if matched
                ),
                "activity_source": str(activity_by_code.at[code, "activity_source"]),
                "activity_pct": float(activity_by_code.at[code, "activity_pct"]),
                "path_context": path.path_context if path is not None else "常态",
                "limit_up_count_5d": int(path.limit_up_count_5d) if path is not None else 0,
                "small_yang_count": int(path.small_yang_count) if path is not None else 0,
                "single_day_max_change": float(path.single_day_max_change) if path is not None else math.nan,
            }
        )
    result = pd.DataFrame(rows)
    if not result.empty:
        result["change_pct_rank"] = cross_section_percentile(result["change_pct"])
        result["ret_5d_rank"] = cross_section_percentile(result["ret_5d"])
        result["score"] = 0.45 * result["change_pct_rank"] + 0.25 * result["ret_5d_rank"] + 0.30 * result["activity_pct"]
        result = result.sort_values(["score", "activity_pct", "sec_code"], ascending=[False, False, True]).reset_index(drop=True)
    diagnostics["result_count"] = len(result)
    diagnostics["skipped_reason_counts"] = dict(sorted((key, value) for key, value in skipped.items() if value))
    return result, diagnostics


def select_momentum_anomaly_from_context(context: SelectionContext) -> list[Candidate]:
    rows, _ = evaluate_momentum_anomaly(context)
    candidates: list[Candidate] = []
    for rank, row in enumerate(rows.itertuples(index=False), start=1):
        candidates.append(
            Candidate(
                strategy_id=MomentumAnomalyScanner.strategy_id,
                version=MomentumAnomalyScanner.version,
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
class MomentumAnomalyScanner(Scanner):
    strategy_id = "momentum_anomaly"
    version = "v2.0"
    kind = "stock"
    description = "V2 board-aware momentum anomaly path with independent subtype ranking."
    default_params: dict[str, Any] = {}

    def run(self, conn: sqlite3.Connection, trade_date: str) -> list[Candidate]:
        context = build_selection_context(conn, trade_date, mode="close_final")
        self.last_universe_size = len(context.universe)
        return select_momentum_anomaly_from_context(context)
