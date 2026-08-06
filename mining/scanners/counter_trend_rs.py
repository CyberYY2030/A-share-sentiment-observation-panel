from __future__ import annotations

import math
import sqlite3
from collections import Counter
from typing import Any, Mapping

import pandas as pd

from ..selection_context import SelectionContext, build_selection_context
from ..trend_factors import cross_section_percentile, resolve_trend_profile
from . import Candidate, Scanner, register


PRIMARY_BENCHMARK = "000852"
SENSITIVITY_BENCHMARKS = ("000300", "000001", "399001")


def _as_series(values: pd.Series | None, dates: list[str]) -> pd.Series:
    if values is None:
        return pd.Series(index=dates, dtype="float64")
    series = pd.to_numeric(values.copy(), errors="coerce")
    series.index = series.index.map(str)
    return series.reindex(dates)


def evaluate_counter_trend_rs(
    context: SelectionContext,
    *,
    primary_benchmark: pd.Series | None,
    sensitivity_benchmarks: Mapping[str, pd.Series] | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Formal E capability with a fixed primary benchmark and report-only sensitivities."""
    clean_dates = [str(value) for value in context.diagnostics.get("clean_dates", [])]
    dates = list(clean_dates)
    if str(context.trade_date) not in dates:
        dates.append(str(context.trade_date))
    profile = resolve_trend_profile(len(clean_dates))
    output_columns = [
        "sec_code", "sec_name", "reference_price", "rs_ret20", "rs_breakout_pct",
        "separation_raw", "rs_ret20_pct", "rs_breakout_pct_rank", "separation_pct",
        "score", "trend_profile", "primary_benchmark", "sensitivity_returns_20",
    ]
    diagnostics: dict[str, Any] = {
        "primary_benchmark": PRIMARY_BENCHMARK,
        "trend_profile": profile.profile_id if profile else None,
        "result_count": 0,
        "skipped_reason_counts": {},
    }
    if context.data_status != "ready" or profile is None or context.bars.empty:
        diagnostics["skipped_reason_counts"] = {
            "insufficient_history" if profile is None else "data_unavailable": len(context.universe)
        }
        return pd.DataFrame(columns=output_columns), diagnostics
    if len(dates) < max(61, profile.long_window + 1):
        diagnostics["skipped_reason_counts"] = {"insufficient_clean_history": len(context.universe)}
        return pd.DataFrame(columns=output_columns), diagnostics
    benchmark = _as_series(primary_benchmark, dates)
    if benchmark.isna().any() or (benchmark <= 0).any():
        diagnostics["skipped_reason_counts"] = {"primary_benchmark_missing_or_invalid": len(context.universe)}
        return pd.DataFrame(columns=output_columns), diagnostics
    benchmark_ret20 = float(benchmark.iloc[-1] / benchmark.iloc[-21] - 1.0)
    diagnostics["primary_benchmark_return_20"] = benchmark_ret20
    if benchmark_ret20 >= 0:
        diagnostics["skipped_reason_counts"] = {"primary_benchmark_not_weak": len(context.universe)}
        return pd.DataFrame(columns=output_columns), diagnostics
    benchmark_returns = benchmark.pct_change(fill_method=None)
    down_dates = benchmark_returns.iloc[-20:][benchmark_returns.iloc[-20:] < 0].index.tolist()
    sensitivity_returns = {
        code: float(series.iloc[-1] / series.iloc[-21] - 1.0)
        for code, values in (sensitivity_benchmarks or {}).items()
        for series in [_as_series(values, dates)]
        if not series.isna().any() and (series > 0).all()
    }
    names = (
        context.universe.assign(sec_code=context.universe["sec_code"].astype(str).str.zfill(6))
        .set_index("sec_code")["sec_name"]
        .astype(str)
        .to_dict()
        if "sec_name" in context.universe.columns
        else {}
    )
    skipped = Counter()
    rows: list[dict[str, Any]] = []
    for code, frame in context.bars.groupby(context.bars["sec_code"].astype(str).str.zfill(6), sort=True):
        indexed = frame.assign(trade_date=frame["trade_date"].astype(str)).set_index("trade_date").reindex(dates)
        close = pd.to_numeric(indexed.get("adj_close"), errors="coerce")
        change_pct = pd.to_numeric(indexed.get("change_pct"), errors="coerce")
        if close.isna().iloc[-max(61, profile.long_window + 1) :].any() or change_pct.isna().iloc[-1]:
            skipped["stock_window_missing"] += 1
            continue
        stock_ret20 = float(close.iloc[-1] / close.iloc[-21] - 1.0)
        if stock_ret20 <= 0:
            skipped["stock_not_absolutely_up"] += 1
            continue
        rs = close / benchmark
        historical_rs_high = float(rs.iloc[-61:-1].max())
        rs_breakout_pct = float(rs.iloc[-1] / historical_rs_high - 1.0) if historical_rs_high > 0 else math.nan
        ma20_rs = rs.rolling(20, min_periods=20).mean()
        rs_trend_ok = float(rs.iloc[-1]) > float(ma20_rs.iloc[-1]) and float(ma20_rs.iloc[-1]) > float(ma20_rs.iloc[-11])
        ma_middle = close.rolling(profile.middle_window, min_periods=profile.middle_window).mean()
        ma_long = close.rolling(profile.long_window, min_periods=profile.long_window).mean()
        price_trend_ok = float(close.iloc[-1]) > float(ma_middle.iloc[-1]) and float(close.iloc[-1]) > float(ma_long.iloc[-1])
        stock_returns = close.pct_change(fill_method=None).reindex(down_dates)
        if stock_returns.isna().any():
            skipped["benchmark_down_day_alignment_missing"] += 1
            continue
        separation_raw = float((stock_returns - benchmark_returns.reindex(down_dates)).mean()) if down_dates else 0.0
        qualifies = (
            rs_breakout_pct >= 0.001
            and rs_trend_ok
            and price_trend_ok
            and float(change_pct.iloc[-1]) > 0
        )
        if not qualifies:
            skipped["counter_trend_gate_failed"] += 1
            continue
        rows.append(
            {
                "sec_code": code,
                "sec_name": names.get(code, code),
                "reference_price": float(close.iloc[-1]),
                "rs_ret20": float(rs.iloc[-1] / rs.iloc[-21] - 1.0),
                "rs_breakout_pct": rs_breakout_pct,
                "separation_raw": separation_raw,
                "trend_profile": profile.profile_id,
                "primary_benchmark": PRIMARY_BENCHMARK,
                "sensitivity_returns_20": sensitivity_returns,
            }
        )
    result = pd.DataFrame(rows, columns=output_columns[:6] + output_columns[9:])
    if not result.empty:
        result["rs_ret20_pct"] = cross_section_percentile(result["rs_ret20"])
        result["rs_breakout_pct_rank"] = cross_section_percentile(result["rs_breakout_pct"])
        result["separation_pct"] = cross_section_percentile(result["separation_raw"])
        result["score"] = 0.40 * result["rs_ret20_pct"] + 0.35 * result["rs_breakout_pct_rank"] + 0.25 * result["separation_pct"]
        result = result.sort_values(["score", "sec_code"], ascending=[False, True]).reset_index(drop=True)
    diagnostics["sensitivity_returns_20"] = sensitivity_returns
    diagnostics["result_count"] = len(result)
    diagnostics["skipped_reason_counts"] = dict(sorted((key, value) for key, value in skipped.items() if value))
    return result.reindex(columns=output_columns), diagnostics


def _load_benchmark_close(conn: sqlite3.Connection, dates: list[str], code: str) -> pd.Series:
    if not dates:
        return pd.Series(dtype="float64")
    marks = ",".join("?" for _ in dates)
    frame = pd.read_sql_query(
        f"""
        SELECT trade_date, close
        FROM ash.kline_daily
        WHERE sec_type='index' AND sec_code=? AND trade_date IN ({marks})
        """,
        conn,
        params=[str(code), *dates],
    )
    return pd.to_numeric(frame.set_index("trade_date")["close"], errors="coerce") if not frame.empty else pd.Series(dtype="float64")


def select_counter_trend_rs_from_context(
    context: SelectionContext,
    *,
    primary_benchmark: pd.Series | None,
    sensitivity_benchmarks: Mapping[str, pd.Series] | None = None,
) -> list[Candidate]:
    rows, _ = evaluate_counter_trend_rs(
        context,
        primary_benchmark=primary_benchmark,
        sensitivity_benchmarks=sensitivity_benchmarks,
    )
    return [
        Candidate(
            strategy_id=CounterTrendRsScanner.strategy_id,
            version=CounterTrendRsScanner.version,
            trade_date=context.trade_date,
            sec_type="stock",
            sec_code=row.sec_code,
            sec_name=row.sec_name,
            entry_price=float(row.reference_price),
            features={key: getattr(row, key) for key in rows.columns if key not in {"sec_code", "sec_name", "reference_price"}},
            rank=rank,
        )
        for rank, row in enumerate(rows.itertuples(index=False), start=1)
    ]


@register
class CounterTrendRsScanner(Scanner):
    strategy_id = "counter_trend_rs"
    version = "v2.0"
    kind = "stock"
    description = "V2 counter-trend relative-strength scanner with fixed 000852 primary benchmark."
    default_params: dict[str, Any] = {}

    def run(self, conn: sqlite3.Connection, trade_date: str) -> list[Candidate]:
        context = build_selection_context(conn, trade_date, mode="close_final")
        self.last_universe_size = len(context.universe)
        dates = [str(value) for value in context.diagnostics.get("clean_dates", [])]
        if str(trade_date) not in dates:
            dates.append(str(trade_date))
        primary = _load_benchmark_close(conn, dates, PRIMARY_BENCHMARK)
        sensitivity = {code: _load_benchmark_close(conn, dates, code) for code in SENSITIVITY_BENCHMARKS}
        return select_counter_trend_rs_from_context(
            context,
            primary_benchmark=primary,
            sensitivity_benchmarks=sensitivity,
        )
