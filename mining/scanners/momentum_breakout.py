from __future__ import annotations

import sqlite3

import pandas as pd

from ..db import list_stock_trade_dates
from ..features import (
    high_breakout_from_close_pct,
    high_breakout_from_open_pct,
    upper_shadow_pct,
)
from ..universe import build_universe
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


def _load_rps_exclusion_codes(
    conn: sqlite3.Connection,
    trade_date: str,
    universe: pd.DataFrame,
) -> set[str]:
    from .rps_stock import (
        RpsStockScanner,
        select_candidates_from_universe as select_rps_candidates,
    )

    rps_scanner = RpsStockScanner()
    rps_candidates = select_rps_candidates(
        conn=conn,
        trade_date=trade_date,
        universe=universe,
        params=rps_scanner.params,
        strategy_id=rps_scanner.strategy_id,
        version=rps_scanner.version,
    )
    return {candidate.sec_code for candidate in rps_candidates}


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
        if rps_exclusion_codes is None:
            rps_exclusion_codes = _load_rps_exclusion_codes(conn, trade_date, universe)
        early_strength_pool = early_strength_pool[
            ~early_strength_pool["sec_code"].isin(rps_exclusion_codes)
        ].copy()
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
