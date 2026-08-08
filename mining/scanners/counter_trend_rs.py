from __future__ import annotations

import sqlite3
from collections import Counter
from typing import Any

import pandas as pd

from ..selection_context import SelectionContext, build_selection_context
from ..capabilities import SCREENING_DEFINITION_VERSION
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


def evaluate_counter_trend_rs(context: SelectionContext) -> tuple[pd.DataFrame, dict[str, Any]]:
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
    benchmark = _as_series(context.benchmark_closes.get(PRIMARY_BENCHMARK), dates)
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
        for code, values in context.benchmark_closes.items()
        if code in SENSITIVITY_BENCHMARKS
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
    bars = context.bars.copy()
    bars["sec_code"] = bars["sec_code"].astype(str).str.zfill(6)
    bars["trade_date"] = bars["trade_date"].astype(str)
    codes = context.universe["sec_code"].astype(str).str.zfill(6).drop_duplicates().tolist()
    close = bars.pivot(index="trade_date", columns="sec_code", values="adj_close").reindex(index=dates, columns=codes)
    change_pct = bars.pivot(index="trade_date", columns="sec_code", values="change_pct").reindex(index=dates, columns=codes)
    close = close.apply(pd.to_numeric, errors="coerce")
    change_pct = change_pct.apply(pd.to_numeric, errors="coerce")
    required_window = max(61, profile.long_window + 1)
    complete_window = close.iloc[-required_window:].notna().all(axis=0) & change_pct.iloc[-1].notna()
    stock_ret20 = close.iloc[-1] / close.iloc[-21] - 1.0
    stock_up = stock_ret20.gt(0)
    rs = close.div(benchmark, axis="index")
    historical_rs_high = rs.iloc[-61:-1].max(axis=0)
    rs_breakout_pct = rs.iloc[-1] / historical_rs_high - 1.0
    ma20_rs = rs.rolling(20, min_periods=20).mean()
    rs_trend_ok = rs.iloc[-1].gt(ma20_rs.iloc[-1]) & ma20_rs.iloc[-1].gt(ma20_rs.iloc[-11])
    ma_middle = close.rolling(profile.middle_window, min_periods=profile.middle_window).mean()
    ma_long = close.rolling(profile.long_window, min_periods=profile.long_window).mean()
    price_trend_ok = close.iloc[-1].gt(ma_middle.iloc[-1]) & close.iloc[-1].gt(ma_long.iloc[-1])
    stock_returns = close.pct_change(fill_method=None)
    aligned = stock_returns.reindex(down_dates).notna().all(axis=0) if down_dates else pd.Series(True, index=codes)
    separation_raw = (
        stock_returns.reindex(down_dates).sub(benchmark_returns.reindex(down_dates), axis="index").mean(axis=0)
        if down_dates
        else pd.Series(0.0, index=codes)
    )
    qualifies = (
        rs_breakout_pct.ge(0.001)
        & rs_trend_ok
        & price_trend_ok
        & change_pct.iloc[-1].gt(0)
    )
    eligible = complete_window & stock_up & aligned & qualifies
    skipped = Counter()
    skipped["stock_window_missing"] = int((~complete_window).sum())
    skipped["stock_not_absolutely_up"] = int((complete_window & ~stock_up).sum())
    skipped["benchmark_down_day_alignment_missing"] = int((complete_window & stock_up & ~aligned).sum())
    skipped["counter_trend_gate_failed"] = int((complete_window & stock_up & aligned & ~qualifies).sum())
    result = pd.DataFrame(
        {
            "sec_code": [code for code in codes if bool(eligible.get(code, False))],
        }
    )
    if not result.empty:
        result["sec_name"] = result["sec_code"].map(names).fillna(result["sec_code"])
        result["reference_price"] = result["sec_code"].map(close.iloc[-1])
        result["rs_ret20"] = result["sec_code"].map(rs.iloc[-1] / rs.iloc[-21] - 1.0)
        result["rs_breakout_pct"] = result["sec_code"].map(rs_breakout_pct)
        result["separation_raw"] = result["sec_code"].map(separation_raw)
        result["trend_profile"] = profile.profile_id
        result["primary_benchmark"] = PRIMARY_BENCHMARK
        result["sensitivity_returns_20"] = [sensitivity_returns] * len(result)
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


def select_counter_trend_rs_from_context(context: SelectionContext) -> list[Candidate]:
    rows, _ = evaluate_counter_trend_rs(context)
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
    version = SCREENING_DEFINITION_VERSION
    kind = "stock"
    description = "V2 counter-trend relative-strength scanner with fixed 000852 primary benchmark."
    default_params: dict[str, Any] = {}

    def run(self, conn: sqlite3.Connection, trade_date: str) -> list[Candidate]:
        context = build_selection_context(conn, trade_date, mode="close_final")
        self.last_universe_size = len(context.universe)
        return select_counter_trend_rs_from_context(context)
