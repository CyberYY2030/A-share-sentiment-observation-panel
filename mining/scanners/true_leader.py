from __future__ import annotations

import sqlite3
from typing import Any

import pandas as pd

from ..db import list_stock_trade_dates
from ..features import rolling_new_high, rps
from ..universe import build_universe
from . import Candidate, Scanner, register


def load_index_series(conn: sqlite3.Connection, benchmark_code: str, dates: list[str]) -> pd.DataFrame:
    if not dates:
        return pd.DataFrame(columns=["trade_date", "close", "change_pct"])
    date_ph = ",".join("?" for _ in dates)
    return pd.read_sql_query(
        f"""
        SELECT trade_date, close, change_pct
        FROM ash.kline_daily
        WHERE sec_type='index'
          AND sec_code=?
          AND trade_date IN ({date_ph})
        ORDER BY trade_date
        """,
        conn,
        params=[benchmark_code, *dates],
    )


def _load_stock_history(conn: sqlite3.Connection, sec_codes: list[str], dates: list[str]) -> pd.DataFrame:
    if not sec_codes or not dates:
        return pd.DataFrame()
    code_ph = ",".join("?" for _ in sec_codes)
    date_ph = ",".join("?" for _ in dates)
    return pd.read_sql_query(
        f"""
        SELECT sec_code, trade_date, close, change_pct
        FROM ash.kline_daily
        WHERE sec_type='stock'
          AND sec_code IN ({code_ph})
          AND trade_date IN ({date_ph})
        """,
        conn,
        params=[*sec_codes, *dates],
    )


def _separation_metrics(
    change_pivot: pd.DataFrame,
    index_change: pd.Series,
    min_down_days: int,
) -> tuple[pd.Series, pd.Series, int, bool]:
    down_dates = index_change[index_change < 0].dropna().index.tolist()
    down_days_used = len(down_dates)
    insufficient = down_days_used < min_down_days
    if down_days_used == 0:
        columns = change_pivot.columns
        return (
            pd.Series(0.0, index=columns, name="separation_strength"),
            pd.Series(0.0, index=columns, name="separation_ratio"),
            0,
            insufficient,
        )

    stock_down_changes = change_pivot.reindex(down_dates)
    index_down_changes = index_change.reindex(down_dates)
    excess = stock_down_changes.sub(index_down_changes, axis=0)
    separation_strength = excess.mean(axis=0).fillna(0.0)
    separation_strength.name = "separation_strength"
    separation_ratio = (stock_down_changes > 0).sum(axis=0) / down_days_used
    separation_ratio.name = "separation_ratio"
    return separation_strength, separation_ratio, down_days_used, insufficient


def select_candidates_from_universe(
    conn: sqlite3.Connection,
    trade_date: str,
    universe: pd.DataFrame,
    params: dict[str, Any],
    strategy_id: str,
    version: str,
) -> list[Candidate]:
    if universe.empty:
        return []

    lookback = int(params["lookback"])
    fallback_lookback = int(params["fallback_lookback"])
    history_limit = max(lookback, fallback_lookback) + 1
    all_dates = list_stock_trade_dates(conn, end_date=trade_date, limit=history_limit, include_end=True)
    if len(all_dates) < lookback + 1:
        return []
    dates = all_dates[-(lookback + 1) :]

    sec_codes = universe["sec_code"].astype(str).tolist()
    history = _load_stock_history(conn, sec_codes, all_dates)
    if history.empty:
        return []

    close_pivot_all = (
        history.pivot_table(index="trade_date", columns="sec_code", values="close", aggfunc="last")
        .reindex(all_dates)
        .sort_index()
    )
    change_pivot_all = (
        history.pivot_table(index="trade_date", columns="sec_code", values="change_pct", aggfunc="last")
        .reindex(all_dates)
        .sort_index()
    )
    close_pivot = close_pivot_all.reindex(dates)
    change_pivot = change_pivot_all.reindex(dates)
    if close_pivot.empty or trade_date not in close_pivot.index:
        return []

    ret_n = ((close_pivot.iloc[-1] / close_pivot.iloc[0]) - 1.0) * 100.0
    ret_n.name = "ret_N"
    rps_n = rps(ret_n)
    rps_n.name = "rps_N"

    benchmark = str(params["benchmark_code"])
    index_df = load_index_series(conn, benchmark, all_dates)
    index_change_all = pd.to_numeric(
        index_df.set_index("trade_date").reindex(all_dates)["change_pct"], errors="coerce"
    )
    index_change = index_change_all.reindex(dates)
    min_down_days = int(params["min_down_days"])
    separation_strength, separation_ratio, down_days_used, insufficient = _separation_metrics(
        change_pivot, index_change, min_down_days
    )
    if insufficient and fallback_lookback > lookback and len(all_dates) > len(dates):
        fallback_dates = all_dates[-min(fallback_lookback + 1, len(all_dates)) :]
        fallback_strength, fallback_ratio, fallback_down_days, fallback_insufficient = _separation_metrics(
            change_pivot_all.reindex(fallback_dates),
            index_change_all.reindex(fallback_dates),
            min_down_days,
        )
        separation_strength = fallback_strength
        separation_ratio = fallback_ratio
        down_days_used = fallback_down_days
        insufficient = fallback_insufficient

    today_index_change = index_change_all.loc[trade_date] if trade_date in index_change_all.index else float("nan")
    today_change = pd.to_numeric(change_pivot_all.loc[trade_date], errors="coerce")
    divergence_today = (today_index_change < 0) & (today_change > float(params["sep_today_min"]))
    divergence_today.name = "divergence_today"

    new_high_window = int(params["new_high_window"])
    is_new_high = close_pivot.apply(lambda series: rolling_new_high(series, new_high_window))
    is_new_high.name = "is_new_high"

    result = universe[["sec_code", "sec_name", "close", "amount"]].copy()
    result["amount"] = pd.to_numeric(result["amount"], errors="coerce")
    result["ret_N"] = result["sec_code"].map(ret_n)
    result["rps_N"] = result["sec_code"].map(rps_n)
    result["separation_strength"] = result["sec_code"].map(separation_strength).fillna(0.0)
    result["separation_ratio"] = result["sec_code"].map(separation_ratio).fillna(0.0)
    result["divergence_today"] = result["sec_code"].map(divergence_today).fillna(False).astype(bool)
    result["is_new_high"] = result["sec_code"].map(is_new_high).fillna(False).astype(bool)
    result["amount_band_ok"] = result["amount"].between(
        float(params["amt_band_lo"]), float(params["amt_band_hi"]), inclusive="both"
    )
    result["over_extended"] = pd.to_numeric(result["ret_N"], errors="coerce") > float(params["ret_cap_soft"])
    cap_span = float(params["ret_cap_hard"]) - float(params["ret_cap_soft"])
    if cap_span <= 0:
        result["over_extension_penalty"] = 0.0
    else:
        result["over_extension_penalty"] = (
            (pd.to_numeric(result["ret_N"], errors="coerce") - float(params["ret_cap_soft"])) / cap_span
        ).clip(lower=0, upper=1) * float(params["w_ext"])
    effective_sep = result["separation_strength"] * (float(params["insufficient_weight"]) if insufficient else 1.0)
    result["score"] = (
        float(params["w_sep"]) * effective_sep
        + float(params["w_high"]) * result["is_new_high"].astype(float)
        + float(params["w_div"]) * result["divergence_today"].astype(float)
        + float(params["w_amt"]) * result["amount_band_ok"].astype(float)
        - result["over_extension_penalty"].fillna(0.0)
    )
    result["benchmark"] = benchmark
    result["down_days_used"] = down_days_used
    result["separation_insufficient"] = insufficient

    hard_cap = pd.to_numeric(result["ret_N"], errors="coerce") <= float(params["ret_cap_hard"])
    strength_gate = pd.to_numeric(result["rps_N"], errors="coerce") >= float(params["strength_min"])
    if insufficient:
        sep_gate = pd.Series(True, index=result.index)
    else:
        sep_gate = pd.to_numeric(result["separation_strength"], errors="coerce") >= float(params["sep_excess_min"])

    selected = result[strength_gate & sep_gate & hard_cap].copy()
    if selected.empty:
        return []

    selected = selected.sort_values(
        by=["score", "separation_strength", "ret_N", "sec_code"],
        ascending=[False, False, False, True],
    ).head(int(params["top_n"]))

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
                    "ret_N": float(row.ret_N),
                    "rps_N": float(row.rps_N),
                    "close": float(row.close),
                    "amount": float(row.amount),
                    "separation_strength": float(row.separation_strength),
                    "separation_ratio": float(row.separation_ratio),
                    "down_days_used": int(row.down_days_used),
                    "separation_insufficient": bool(row.separation_insufficient),
                    "divergence_today": bool(row.divergence_today),
                    "is_new_high": bool(row.is_new_high),
                    "amount_band_ok": bool(row.amount_band_ok),
                    "over_extended": bool(row.over_extended),
                    "over_extension_penalty": float(row.over_extension_penalty),
                    "benchmark": row.benchmark,
                    "score": float(row.score),
                },
                rank=rank,
            )
        )
    return candidates


@register
class TrueLeaderScanner(Scanner):
    strategy_id = "true_leader"
    version = "v1.1"
    kind = "stock"
    description = "Stage-strength leader scanner with continuous index-down separation and extension cap."
    default_params = {
        "lookback": 20,
        "fallback_lookback": 40,
        "strength_min": 70.0,
        "benchmark_code": "000852",
        "sep_excess_min": 0.0,
        "min_down_days": 3,
        "insufficient_weight": 0.5,
        "sep_today_min": 0.0,
        "new_high_window": 20,
        # Amount band is configurable because the useful liquidity band shifts with market regime.
        "amt_band_lo": 2_000_000_000,
        "amt_band_hi": 8_000_000_000,
        "ret_cap_soft": 60.0,
        "ret_cap_hard": 100.0,
        "w_ext": 2.0,
        "top_n": 20,
        "w_sep": 3.0,
        "w_high": 1.0,
        "w_div": 1.0,
        "w_amt": 1.0,
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