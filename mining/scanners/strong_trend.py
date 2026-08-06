from __future__ import annotations

import math
import sqlite3
from collections import Counter
from dataclasses import dataclass
from typing import Any

import pandas as pd

from ..features import board_kind, is_limit_up
from ..selection_context import SelectionContext, build_selection_context
from ..trend_factors import TrendProfile, cross_section_percentile, evaluate_trend_structure, resolve_trend_profile
from . import Candidate, Scanner, register


@dataclass
class StrongTrendEvaluation:
    rows: pd.DataFrame
    trend_profile: TrendProfile | None
    diagnostics: dict[str, Any]


def _stock_returns(bars: pd.DataFrame, profile: TrendProfile) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    expected_dates = sorted(bars["trade_date"].astype(str).unique())
    for sec_code, frame in bars.groupby("sec_code", sort=True):
        close = pd.to_numeric(
            frame.assign(trade_date=frame["trade_date"].astype(str)).set_index("trade_date")["adj_close"], errors="coerce"
        ).reindex(expected_dates)
        if len(close) < profile.required_stock_bars or not close.tail(profile.required_stock_bars).notna().all():
            rows.append({"sec_code": sec_code, "rps_recent_raw": pd.NA, "rps_prior_raw": pd.NA})
            continue
        current = float(close.iloc[-1])
        recent_start = float(close.iloc[-21])
        prior_start = float(close.iloc[-1 - profile.long_window])
        prior_end = float(close.iloc[-21])
        rows.append(
            {
                "sec_code": sec_code,
                "rps_recent_raw": current / recent_start - 1.0 if recent_start > 0 else pd.NA,
                "rps_prior_raw": prior_end / prior_start - 1.0 if prior_start > 0 else pd.NA,
            }
        )
    return pd.DataFrame(rows)


def _separation_raw(
    bars: pd.DataFrame,
    benchmark_returns: pd.Series | None,
    *,
    min_down_days: int,
) -> tuple[pd.DataFrame, bool, int]:
    empty = pd.DataFrame(columns=["sec_code", "separation_raw", "separation_ratio"])
    if benchmark_returns is None or benchmark_returns.empty:
        return empty, False, 0
    benchmark = pd.to_numeric(benchmark_returns, errors="coerce").dropna()
    first_trade_date = bars["trade_date"].astype(str).min() if not bars.empty else None
    if first_trade_date in benchmark.index:
        benchmark = benchmark.drop(index=first_trade_date)
    down_dates = benchmark[benchmark < 0].index.tolist()
    if len(down_dates) < int(min_down_days):
        return empty, False, len(down_dates)

    rows: list[dict[str, object]] = []
    for sec_code, frame in bars.groupby("sec_code", sort=True):
        close = pd.to_numeric(frame.set_index("trade_date")["adj_close"], errors="coerce")
        stock_returns = close.pct_change(fill_method=None).reindex(down_dates)
        if stock_returns.isna().any():
            rows.append({"sec_code": sec_code, "separation_raw": pd.NA, "separation_ratio": pd.NA})
            continue
        benchmark_down = benchmark.reindex(down_dates)
        rows.append(
            {
                "sec_code": sec_code,
                "separation_raw": float((stock_returns - benchmark_down).mean()),
                "separation_ratio": float((stock_returns > 0).mean()),
            }
        )
    return pd.DataFrame(rows), True, len(down_dates)


def _path_context(bars: pd.DataFrame, sec_code: str) -> str:
    frame = bars[bars["sec_code"].astype(str).eq(str(sec_code))].sort_values("trade_date", kind="stable").tail(5)
    changes = pd.to_numeric(frame.get("change_pct"), errors="coerce")
    board = board_kind(str(sec_code))
    limit_up_count = (
        int(
            sum(
                is_limit_up(row.close, row.pre_close, board)
                for row in frame[["close", "pre_close"]].itertuples(index=False)
            )
        )
        if {"close", "pre_close"}.issubset(frame.columns)
        else 0
    )
    small_yang_count = int((changes.gt(0) & changes.lt(5.0)).sum())
    max_change = float(changes.max()) if changes.notna().any() else math.nan
    return f"limit_up_5d={limit_up_count};small_yang_5d={small_yang_count};max_change_5d={max_change:.2f}"


def evaluate_strong_trend(
    context: SelectionContext,
    *,
    benchmark_returns: pd.Series | None = None,
    min_down_days: int = 3,
) -> StrongTrendEvaluation:
    """Pure A-capability selection: apply hard gates first, then rank same-unit percentiles."""
    clean_sessions = len(context.diagnostics.get("clean_dates", []))
    profile = resolve_trend_profile(clean_sessions)
    diagnostics: dict[str, Any] = {
        "trend_profile": profile.profile_id if profile else None,
        "universe_count": len(context.universe),
        "eligible_count": 0,
        "result_count": 0,
        "skipped_reason_counts": {},
    }
    if context.data_status != "ready" or profile is None or context.bars.empty:
        diagnostics["skipped_reason_counts"] = {
            "insufficient_history" if profile is None else "data_unavailable": len(context.universe)
        }
        return StrongTrendEvaluation(pd.DataFrame(), profile, diagnostics)

    structure = evaluate_trend_structure(context.bars, profile)
    returns = _stock_returns(context.bars, profile)
    separation, separation_available, down_days_used = _separation_raw(
        context.bars, benchmark_returns, min_down_days=min_down_days
    )
    base = context.universe.copy()
    base["sec_code"] = base["sec_code"].astype(str).str.zfill(6)
    base = base.merge(structure, on="sec_code", how="left").merge(returns, on="sec_code", how="left")
    if separation_available:
        base = base.merge(separation, on="sec_code", how="left")
    else:
        base["separation_raw"] = pd.NA
        base["separation_ratio"] = pd.NA

    skipped = Counter()
    gate_history = base["history_sufficient"].fillna(False).astype(bool)
    skipped["insufficient_stock_history"] = int((~gate_history).sum())
    gate_structure = base["trend_structure_pass"].fillna(False).astype(bool)
    skipped["trend_structure_failed"] = int((gate_history & ~gate_structure).sum())
    gate_near_high = pd.to_numeric(base["near_high_ratio"], errors="coerce").ge(0.85)
    skipped["near_high_failed"] = int((gate_history & gate_structure & ~gate_near_high).sum())
    liquidity = pd.to_numeric(base.get("liquidity_pct"), errors="coerce")
    liquidity_globally_unavailable = liquidity.dropna().empty
    gate_liquidity = pd.Series(True, index=base.index) if liquidity_globally_unavailable else liquidity.ge(0.4)
    skipped["liquidity_failed"] = int((gate_history & gate_structure & gate_near_high & ~gate_liquidity).sum())

    eligible = base[gate_history & gate_structure & gate_near_high & gate_liquidity].copy()
    required_components = ["rps_recent_raw", "rps_prior_raw", "near_high_ratio"]
    if separation_available:
        required_components.append("separation_raw")
    missing_components = eligible[required_components].isna().any(axis=1)
    skipped["missing_required_factor"] = int(missing_components.sum())
    eligible = eligible[~missing_components].copy()
    diagnostics.update(
        {
            "separation_available": separation_available,
            "down_days_used": down_days_used,
            "liquidity_globally_unavailable": liquidity_globally_unavailable,
            "skipped_reason_counts": {key: value for key, value in sorted(skipped.items()) if value},
        }
    )
    if eligible.empty:
        return StrongTrendEvaluation(eligible, profile, diagnostics)

    eligible["rps_recent_pct"] = cross_section_percentile(eligible["rps_recent_raw"])
    eligible["rps_prior_pct"] = cross_section_percentile(eligible["rps_prior_raw"])
    eligible["near_high_pct"] = cross_section_percentile(eligible["near_high_ratio"])
    weights = {"rps_recent_pct": 0.35, "rps_prior_pct": 0.25, "near_high_pct": 0.15}
    if separation_available:
        eligible["separation_pct"] = cross_section_percentile(eligible["separation_raw"])
        weights["separation_pct"] = 0.25
    weight_total = sum(weights.values())
    weights = {key: value / weight_total for key, value in weights.items()}
    eligible["score"] = sum(eligible[column] * weight for column, weight in weights.items())
    eligible["score_components_used"] = ",".join(weights)
    eligible = eligible.sort_values(["score", "rps_recent_pct", "sec_code"], ascending=[False, False, True]).reset_index(drop=True)
    strong_count = max(1, math.ceil(len(eligible) * 0.2))
    eligible["strength_tier"] = "趋势成型中"
    eligible.loc[: strong_count - 1, "strength_tier"] = "强趋势"
    eligible["path_context"] = eligible["sec_code"].map(lambda code: _path_context(context.bars, code))
    diagnostics["eligible_count"] = len(eligible)
    diagnostics["result_count"] = len(eligible)
    return StrongTrendEvaluation(eligible, profile, diagnostics)


def _load_benchmark_returns(conn: sqlite3.Connection, dates: list[str], benchmark_code: str) -> pd.Series | None:
    if not dates:
        return None
    marks = ",".join("?" for _ in dates)
    index_bars = pd.read_sql_query(
        f"""
        SELECT trade_date, close
        FROM ash.kline_daily
        WHERE sec_type='index' AND sec_code=? AND trade_date IN ({marks})
        ORDER BY trade_date
        """,
        conn,
        params=[str(benchmark_code), *dates],
    )
    if index_bars.empty:
        return None
    close = pd.to_numeric(index_bars.set_index("trade_date")["close"], errors="coerce").reindex(dates)
    return close.pct_change(fill_method=None)


def select_from_context(context: SelectionContext, *, benchmark_returns: pd.Series | None = None) -> list[Candidate]:
    evaluation = evaluate_strong_trend(context, benchmark_returns=benchmark_returns)
    rows = evaluation.rows
    if rows.empty:
        return []
    candidates: list[Candidate] = []
    for rank, row in enumerate(rows.itertuples(index=False), start=1):
        candidates.append(
            Candidate(
                strategy_id=StrongTrendScanner.strategy_id,
                version=StrongTrendScanner.version,
                trade_date=context.trade_date,
                sec_type="stock",
                sec_code=row.sec_code,
                sec_name=row.sec_name,
                entry_price=float(row.close),
                features={
                    "reference_price": float(row.close),
                    "trend_profile": evaluation.trend_profile.profile_id if evaluation.trend_profile else None,
                    "rps_recent_pct": float(row.rps_recent_pct),
                    "rps_prior_pct": float(row.rps_prior_pct),
                    "separation_pct": None if pd.isna(getattr(row, "separation_pct", pd.NA)) else float(row.separation_pct),
                    "near_high_pct": float(row.near_high_pct),
                    "score": float(row.score),
                    "score_components_used": row.score_components_used,
                    "strength_tier": row.strength_tier,
                    "path_context": row.path_context,
                    "liquidity_pct": None if pd.isna(row.liquidity_pct) else float(row.liquidity_pct),
                    "separation_ratio": None if pd.isna(row.separation_ratio) else float(row.separation_ratio),
                },
                rank=rank,
            )
        )
    return candidates


@register
class StrongTrendScanner(Scanner):
    strategy_id = "strong_trend"
    version = "v2.0"
    kind = "stock"
    description = "V2 strong-trend capability using shared adjusted selection context."
    default_params = {"benchmark_code": "000852", "min_down_days": 3}

    def run(self, conn: sqlite3.Connection, trade_date: str) -> list[Candidate]:
        context = build_selection_context(conn, trade_date, mode="close_final")
        self.last_universe_size = len(context.universe)
        benchmark = _load_benchmark_returns(
            conn,
            list(context.diagnostics.get("clean_dates", [])),
            str(self.params["benchmark_code"]),
        )
        return select_from_context(context, benchmark_returns=benchmark)
