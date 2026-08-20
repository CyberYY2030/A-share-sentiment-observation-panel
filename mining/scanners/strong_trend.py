from __future__ import annotations

import copy
import math
import sqlite3
from collections import Counter
from dataclasses import dataclass
from typing import Any

import pandas as pd

from ..features import board_kind
from ..capabilities import SCREENING_DEFINITION_VERSION
from ..selection_context import SelectionContext, build_selection_context
from ..trend_factors import TrendProfile, cross_section_percentile, resolve_trend_profile
from . import Candidate, Scanner, register


FORMAL_A_DEFINITION_VERSION = SCREENING_DEFINITION_VERSION
_A_OUTPUT_COLUMNS = [
    "sec_code", "sec_name", "reference_price", "strength_tier", "rank_band", "score",
    "rps10_pct", "rps20_pct", "near_high_pct", "separation_pct", "liquidity_pct", "activity_pct",
    "ret10", "ret20", "ma10", "ma20", "ma60", "ma20_slope_5", "atr20", "near_high_distance_atr",
    "rolling_high_20", "rolling_high_60", "prior_close_high_20", "near_high_profile", "strength_age",
    "first_seen_as_of", "maturity_evidence", "amount_health", "activity_contraction_at_high",
    "ranking_profile", "path_context", "gate_valid_trade", "gate_structure",
    "gate_ma20_slope", "gate_ma60_slope", "gate_ret10", "gate_ret20", "gate_rps20", "gate_extension",
    "gate_near_high", "gate_liquidity", "gate_path",
]


@dataclass
class StrongTrendEvaluation:
    rows: pd.DataFrame
    trend_profile: TrendProfile | None
    diagnostics: dict[str, Any]


@dataclass
class _AHistory:
    tiers: pd.DataFrame
    gates: dict[str, pd.DataFrame]
    metrics: dict[str, pd.DataFrame]
    atr_coverage: pd.Series


def _complete_tail(series: pd.Series, size: int) -> bool:
    return len(series) >= size and bool(series.iloc[-size:].notna().all())


def _path_contexts(bars: pd.DataFrame) -> dict[str, str]:
    """Build recent path labels once for the entire batch, not once per stock scan."""
    if bars.empty:
        return {}
    recent = bars.copy()
    recent["sec_code"] = recent["sec_code"].astype(str).str.zfill(6)
    recent = recent.sort_values(["sec_code", "trade_date"], kind="stable").groupby("sec_code", sort=False).tail(5)
    close = pd.to_numeric(recent.get("adj_close", recent.get("close")), errors="coerce")
    pre_close = pd.to_numeric(recent.get("adj_pre_close", recent.get("pre_close")), errors="coerce")
    changes = (close / pre_close - 1.0) * 100.0
    limit_rate = recent["sec_code"].map(lambda code: 1.2 if board_kind(code) in {"gem", "star"} else 1.1)
    recent = recent.assign(
        _small_yang=changes.gt(0) & changes.lt(5),
        _limit_up=close.ge((pre_close * limit_rate).round(2) - 0.01),
        _change=changes,
    )
    summary = recent.groupby("sec_code", sort=True).agg(
        limit_up_5d=("_limit_up", "sum"),
        small_yang_5d=("_small_yang", "sum"),
        max_change_5d=("_change", "max"),
    )
    summary["_path_context"] = (
        "limit_up_5d="
        + summary["limit_up_5d"].astype(int).astype(str)
        + ";small_yang_5d="
        + summary["small_yang_5d"].astype(int).astype(str)
        + ";max_change_5d="
        + summary["max_change_5d"].map(lambda value: f"{float(value) if pd.notna(value) else math.nan:.2f}")
    )
    return summary["_path_context"].to_dict()


def _stock_metric_rows(context: SelectionContext, dates: list[str], history: _AHistory) -> pd.DataFrame:
    codes = context.universe["sec_code"].astype(str).str.zfill(6).drop_duplicates().tolist()
    current = dates[-1]
    result = pd.DataFrame({"sec_code": codes})
    result["reference_price"] = history.metrics["close"].loc[current].reindex(codes).to_numpy()
    result["valid_trade"] = history.gates["valid_trade"].loc[current].reindex(codes).to_numpy()
    for name in (
        "ret10", "ret20", "ma10", "ma20", "ma60", "atr20", "rolling_high_20", "rolling_high_60", "high", "activity_pct",
        "prior_close_high_20", "liquidity_raw", "amount_health", "ma20_slope_5", "ma60_slope_10",
        "continuous_60", "continuous_70", "halt_ratio_60", "halt_ratio_70", "resumption_watch",
    ):
        result[name] = history.metrics[name].loc[current].reindex(codes).to_numpy()
    maturity = []
    for code in codes:
        labels = []
        for label, values in (("MA90", history.metrics["ma90"]), ("MA120", history.metrics["ma120"]), ("MA200", history.metrics["ma200"])):
            value = values.at[current, code]
            close = history.metrics["close"].at[current, code]
            if pd.notna(value):
                labels.append(f"{label}={'above' if close > value else 'below'}")
        maturity.append(";".join(labels) or "insufficient_maturity_history")
    result["maturity_evidence"] = maturity
    result["path_context"] = result["sec_code"].map(_path_contexts(context.bars)).fillna("")
    return result


def _separation_raw(bars: pd.DataFrame, benchmark_returns: pd.Series | None, dates: list[str], min_down_days: int) -> tuple[pd.DataFrame, bool, int]:
    if benchmark_returns is None or benchmark_returns.empty:
        return pd.DataFrame(columns=["sec_code", "separation_raw"]), False, 0
    benchmark = pd.to_numeric(benchmark_returns, errors="coerce").reindex(dates).dropna()
    down_dates = benchmark[benchmark < 0].index.tolist()
    if len(down_dates) < int(min_down_days):
        return pd.DataFrame(columns=["sec_code", "separation_raw"]), False, len(down_dates)
    source = "adj_close" if "adj_close" in bars.columns else "close"
    values = bars[["sec_code", "trade_date", source]].copy()
    values["sec_code"] = values["sec_code"].astype(str).str.zfill(6)
    values["trade_date"] = values["trade_date"].astype(str)
    close = values.pivot(index="trade_date", columns="sec_code", values=source).reindex(index=dates)
    stock_returns = close.apply(pd.to_numeric, errors="coerce").pct_change(fill_method=None).reindex(down_dates)
    separation = stock_returns.sub(benchmark.reindex(down_dates), axis="index").mean(axis=0)
    return separation.rename("separation_raw").rename_axis("sec_code").reset_index(), True, len(down_dates)


def _first_failed_gate(row: pd.Series) -> str | None:
    for column, label in (
        ("gate_valid_trade", "valid_trade"), ("gate_structure", "close_ma20_ma60"),
        ("gate_ma20_slope", "ma20_slope_5"), ("gate_ma60_slope", "ma60_slope_10"),
        ("gate_ret10", "ret10"), ("gate_ret20", "ret20"),
        ("gate_rps20", "rps20_pct"), ("gate_near_high", "near_high"),
        ("gate_liquidity", "liquidity_pct"), ("gate_extension", "extension"), ("gate_path", "a_path"),
    ):
        if not bool(row.get(column, False)):
            if column == "gate_near_high" and row.get("near_high_profile") == "missing_atr20":
                return "missing_atr20"
            return label
    return None


def _matrix(
    bars: pd.DataFrame,
    dates: list[str],
    codes: list[str],
    column: str,
    *,
    default: float | str | None = None,
) -> pd.DataFrame:
    if column not in bars.columns:
        return pd.DataFrame(default, index=dates, columns=codes)
    values = bars[["trade_date", "sec_code", column]].copy()
    values["trade_date"] = values["trade_date"].astype(str)
    values["sec_code"] = values["sec_code"].astype(str).str.zfill(6)
    return values.pivot_table(index="trade_date", columns="sec_code", values=column, aggfunc="first").reindex(
        index=dates, columns=codes
    )


def _resumption_watch(status: pd.DataFrame) -> pd.DataFrame:
    """Mark the five usable sessions immediately following a halt longer than ten sessions."""
    halt_statuses = {"confirmed_halt", "provider_halt_placeholder"}
    halted = status.isin(halt_statuses)
    prior_halts = halted.shift(1, fill_value=False).rolling(11, min_periods=11).sum().eq(11)
    first_after_long_halt = ~halted & prior_halts
    result = pd.DataFrame(False, index=status.index, columns=status.columns)
    tradable = ~halted
    for offset in range(5):
        continued = tradable.rolling(offset + 1, min_periods=offset + 1).sum().eq(offset + 1)
        result |= first_after_long_halt.shift(offset, fill_value=False) & continued
    return result


def _a_tier_history(context: SelectionContext, dates: list[str]) -> _AHistory:
    """Re-evaluate frozen A gates at each available session for the display-only age field."""
    codes = context.universe["sec_code"].astype(str).str.zfill(6).drop_duplicates().tolist()
    if not dates or not codes:
        return _AHistory(
            tiers=pd.DataFrame(index=dates, columns=codes, dtype="object"),
            gates={}, metrics={}, atr_coverage=pd.Series(dtype="float64"),
        )
    bars = context.bars.copy()
    bars["sec_code"] = bars["sec_code"].astype(str).str.zfill(6)
    bars = bars[bars["sec_code"].isin(codes)]
    close = _matrix(bars, dates, codes, "adj_close")
    high = _matrix(bars, dates, codes, "adj_high")
    low = _matrix(bars, dates, codes, "adj_low")
    amount = _matrix(bars, dates, codes, "amount")
    if "row_status" in bars.columns:
        status = _matrix(bars, dates, codes, "row_status", default="missing").fillna("missing").astype(str)
        valid_trade = status.eq("valid_trade")
    else:
        status = pd.DataFrame("valid_trade", index=dates, columns=codes)
        valid_trade = close.notna() & high.notna() & low.notna() & amount.gt(0)
    halt = status.isin({"confirmed_halt", "provider_halt_placeholder"})
    resumption_watch = _resumption_watch(status)
    complete_11 = close.notna().astype(int).rolling(11, min_periods=11).sum().eq(11)
    complete_21 = close.notna().astype(int).rolling(21, min_periods=21).sum().eq(21)
    complete_60 = close.notna().astype(int).rolling(60, min_periods=60).sum().eq(60)
    complete_70 = close.notna().astype(int).rolling(70, min_periods=70).sum().eq(70)
    ma10 = close.rolling(10, min_periods=10).mean()
    ma20 = close.rolling(20, min_periods=20).mean()
    ma60 = close.rolling(60, min_periods=60).mean()
    ma90 = close.rolling(90, min_periods=90).mean()
    ma120 = close.rolling(120, min_periods=120).mean()
    ma200 = close.rolling(200, min_periods=200).mean()
    high20 = high.rolling(20, min_periods=20).max()
    high60 = high.rolling(60, min_periods=60).max()
    prior_close_high20 = close.shift(1).rolling(20, min_periods=20).max()
    previous_close = close.shift(1)
    true_range = high - low
    high_gap = (high - previous_close).abs()
    low_gap = (low - previous_close).abs()
    true_range = true_range.where(true_range.ge(high_gap), high_gap)
    true_range = true_range.where(true_range.ge(low_gap), low_gap)
    atr20 = true_range.rolling(20, min_periods=20).mean()
    ret10 = (close / close.shift(10) - 1.0).where(complete_11)
    ret20 = (close / close.shift(20) - 1.0).where(complete_21)
    rps_pool = ret20.notna() & ~resumption_watch
    rps10_pct = ret10.where(rps_pool).rank(axis=1, pct=True, method="average")
    rps20_pct = ret20.where(rps_pool).rank(axis=1, pct=True, method="average")
    liquidity_raw = amount.where(valid_trade).shift(1).rolling(20, min_periods=16).median()
    liquidity_pct = liquidity_raw.where(rps_pool).rank(axis=1, pct=True, method="average")
    activity_ratio = amount.where(valid_trade) / amount.where(valid_trade).shift(1).rolling(20, min_periods=20).mean()
    activity_pct = activity_ratio.where(rps_pool).rank(axis=1, pct=True, method="average")
    valid_amount = amount.where(valid_trade)
    amount_health = valid_amount.rolling(20, min_periods=20).median() / valid_amount.rolling(60, min_periods=60).median()
    atr_coverage = atr20.notna().where(rps_pool).sum(axis=1) / rps_pool.sum(axis=1).replace(0, math.nan)
    use_atr = atr_coverage.ge(0.98)
    thresholds = pd.Series(
        {code: 0.85 if board_kind(code) in {"gem", "star"} else 0.90 for code in codes}
    )
    near_by_board = close.div(high60).ge(thresholds, axis="columns")
    gate_near_high = near_by_board
    near_high_distance_atr = (high60 - close) / atr20
    near_high_raw = close.div(high60)
    extension_limit = pd.Series(
        {code: 1.25 if board_kind(code) in {"gem", "star"} else 1.15 for code in codes}
    )
    gate_extension = close.div(ma20).le(extension_limit, axis="columns")
    shared = (
        valid_trade
        & close.gt(ma20)
        & ma20.gt(ma60)
        & ma20.ge(ma20.shift(5))
        & ma60.ge(ma60.shift(10))
        & rps20_pct.ge(0.90)
        & gate_near_high
        & gate_extension
    )
    continuation = (
        shared
        & complete_70
        & halt.astype(float).rolling(70, min_periods=70).mean().le(0.20)
    )
    continuation_extra = (
        complete_70
        & halt.astype(float).rolling(70, min_periods=70).mean().le(0.20)
    )
    fresh_breakout = (
        shared
        & ~continuation
        & complete_60
        & close.ge(prior_close_high20)
        & close.div(high).ge(0.97)
        & activity_pct.ge(0.80)
        & halt.astype(float).rolling(60, min_periods=60).mean().le(0.20)
    )
    fresh_breakout_extra = (
        complete_60
        & close.ge(prior_close_high20)
        & close.div(high).ge(0.97)
        & activity_pct.ge(0.80)
        & halt.astype(float).rolling(60, min_periods=60).mean().le(0.20)
    )
    tiers = pd.DataFrame(pd.NA, index=dates, columns=codes, dtype="object")
    return _AHistory(
        tiers=tiers.mask(continuation, "continuation").mask(fresh_breakout, "fresh_breakout"),
        gates={
            "valid_trade": valid_trade,
            "structure": close.gt(ma20) & ma20.gt(ma60),
            "ma20_slope": ma20.ge(ma20.shift(5)),
            "ma60_slope": ma60.ge(ma60.shift(10)),
            "ret10": ret10.notna(),
            "ret20": ret20.notna(),
            "rps20": rps20_pct.ge(0.90),
            "near_high": gate_near_high,
            "liquidity": liquidity_pct.notna(),
            "extension": gate_extension,
            "continuation": continuation,
            "fresh_breakout": fresh_breakout,
            "continuation_extra": continuation_extra,
            "fresh_breakout_extra": fresh_breakout_extra,
        },
        metrics={
            "close": close,
            "high": high,
            "ma10": ma10,
            "ma20": ma20,
            "ma60": ma60,
            "ma90": ma90,
            "ma120": ma120,
            "ma200": ma200,
            "ma20_slope_5": ma20 - ma20.shift(5),
            "ma60_slope_10": ma60 - ma60.shift(10),
            "rolling_high_20": high20,
            "rolling_high_60": high60,
            "prior_close_high_20": prior_close_high20,
            "rps10_pct": rps10_pct,
            "rps20_pct": rps20_pct,
            "liquidity_pct": liquidity_pct,
            "activity_pct": activity_pct,
            "liquidity_raw": liquidity_raw,
            "amount_health": amount_health,
            "near_high_distance_atr": near_high_distance_atr,
            "near_high_raw": near_high_raw,
            "near_high_ratio": close.div(high20),
            "halt_ratio_60": halt.astype(float).rolling(60, min_periods=60).mean(),
            "halt_ratio_70": halt.astype(float).rolling(70, min_periods=70).mean(),
            "resumption_watch": resumption_watch,
            "ret10": ret10,
            "ret20": ret20,
            "atr20": atr20,
            "continuous_60": complete_60,
            "continuous_70": complete_70,
            "use_atr": pd.DataFrame({code: use_atr for code in codes}),
        },
        atr_coverage=atr_coverage,
    )


def evaluate_strong_trend(
    context: SelectionContext,
    *,
    min_down_days: int = 3,
) -> StrongTrendEvaluation:
    """Evaluate the frozen current-definition capability A: gates first, ranking second."""
    dates = [str(day) for day in context.diagnostics.get("clean_dates", [])]
    if str(context.trade_date) not in dates:
        dates.append(str(context.trade_date))
    dates = sorted(set(dates))
    profile = resolve_trend_profile(len(dates))
    diagnostics: dict[str, Any] = {
        "trend_profile": profile.profile_id if profile else None,
        "universe_count": len(context.universe), "eligible_count": 0, "result_count": 0,
        "skipped_reason_counts": {}, "definition_version": FORMAL_A_DEFINITION_VERSION,
    }
    if getattr(context, "price_status", context.data_status) == "unavailable" or context.bars.empty:
        diagnostics["skipped_reason_counts"] = {"data_unavailable": len(context.universe)}
        return StrongTrendEvaluation(pd.DataFrame(columns=_A_OUTPUT_COLUMNS), profile, diagnostics)
    cache_key = f"strong_trend:{int(min_down_days)}"
    cached = context.evaluation_cache.get(cache_key)
    if cached is not None:
        return StrongTrendEvaluation(cached.rows.copy(), cached.trend_profile, copy.deepcopy(cached.diagnostics))

    history_a = _a_tier_history(context, dates)
    metrics = _stock_metric_rows(context, dates, history_a)
    base = context.universe.copy()
    base["sec_code"] = base["sec_code"].astype(str).str.zfill(6)
    base = base.merge(metrics, on="sec_code", how="left")
    if base.empty:
        return StrongTrendEvaluation(pd.DataFrame(columns=_A_OUTPUT_COLUMNS), profile, diagnostics)

    rps_pool_mask = base["ret20"].notna() & ~base["resumption_watch"].fillna(False)
    rps_pool = base[rps_pool_mask].copy()
    base["rps10_pct"] = base["ret10"].where(rps_pool_mask).rank(pct=True, method="average")
    base["rps20_pct"] = base["ret20"].where(rps_pool_mask).rank(pct=True, method="average")
    base["liquidity_pct"] = base["liquidity_raw"].where(rps_pool_mask).rank(pct=True, method="average")
    atr_population = base[rps_pool_mask & base["continuous_60"].fillna(False)]
    atr_coverage = float(atr_population["atr20"].notna().mean()) if not atr_population.empty else 0.0
    base["near_high_profile"] = "board_60d"
    base["near_high_distance_atr"] = (base["rolling_high_60"] - base["reference_price"]) / base["atr20"]
    base["near_high_raw"] = base["reference_price"] / base["rolling_high_60"]
    threshold = base["sec_code"].map(lambda code: 0.85 if board_kind(code) in {"gem", "star"} else 0.90)
    base["gate_near_high"] = base["near_high_raw"].ge(threshold)
    base["near_high_pct"] = cross_section_percentile(base["near_high_raw"])

    separation, separation_available, down_days_used = _separation_raw(context.bars, context.benchmark_returns, dates, min_down_days)
    base = base.merge(separation, on="sec_code", how="left")
    base["separation_pct"] = cross_section_percentile(base["separation_raw"]) if separation_available else pd.NA
    base["gate_valid_trade"] = base["valid_trade"].fillna(False)
    base["gate_structure"] = base["reference_price"].gt(base["ma20"]) & base["ma20"].gt(base["ma60"])
    base["gate_ma20_slope"] = base["ma20_slope_5"].ge(0)
    base["gate_ma60_slope"] = base["ma60_slope_10"].ge(0)
    base["gate_ret10"] = base["ret10"].notna()
    base["gate_ret20"] = base["ret20"].notna()
    base["gate_rps20"] = base["rps20_pct"].ge(0.90)
    base["gate_liquidity"] = base["liquidity_pct"].notna()
    extension_limit = base["sec_code"].map(lambda code: 1.25 if board_kind(code) in {"gem", "star"} else 1.15)
    base["gate_extension"] = base["reference_price"].div(base["ma20"]).le(extension_limit)
    shared = base[["gate_valid_trade", "gate_structure", "gate_ma20_slope", "gate_ma60_slope", "gate_ret10", "gate_ret20", "gate_rps20", "gate_near_high", "gate_liquidity", "gate_extension"]].all(axis=1)
    tier_history = history_a.tiers
    latest_tiers = tier_history.reindex(columns=base["sec_code"]).loc[str(context.trade_date)]
    continuation = base["sec_code"].map(latest_tiers).eq("continuation")
    fresh_breakout = base["sec_code"].map(latest_tiers).eq("fresh_breakout")
    base["gate_path"] = (continuation | fresh_breakout) & shared
    base["strength_tier"] = pd.NA
    base.loc[continuation, "strength_tier"] = "continuation"
    base.loc[fresh_breakout, "strength_tier"] = "fresh_breakout"
    base["first_failed_gate"] = base.apply(_first_failed_gate, axis=1)
    base["activity_contraction_at_high"] = base["reference_price"].ge(base["rolling_high_60"]) & base["amount_health"].lt(0.60)
    base["strength_age"] = 0
    base["first_seen_as_of"] = pd.NA
    for index, row in base.loc[base["strength_tier"].notna()].iterrows():
        history = tier_history[str(row["sec_code"])].tail(60).notna().tolist()
        age = 0
        for qualified in reversed(history):
            if not qualified:
                break
            age += 1
        base.loc[index, "strength_age"] = age
        base.loc[index, "first_seen_as_of"] = str(tier_history.index[-age])

    skipped = Counter()
    for gate, label in (("gate_valid_trade", "invalid_or_untradable_today"), ("gate_structure", "short_medium_structure_failed"),
                        ("gate_ma20_slope", "ma20_slope_failed"), ("gate_ma60_slope", "ma60_slope_failed"), ("gate_ret10", "ret10_failed"),
                        ("gate_ret20", "ret20_failed"), ("gate_rps20", "rps20_failed"),
                        ("gate_near_high", "near_high_failed"), ("gate_liquidity", "liquidity_failed"), ("gate_extension", "extension_failed"),
                        ("gate_path", "a_path_failed")):
        skipped[label] = int((~base[gate].fillna(False)).sum())
    skipped["halt_ratio_exceeded"] = int(
        (base["halt_ratio_60"].gt(0.20) | base["halt_ratio_70"].gt(0.20)).sum()
    )
    skipped["resumption_watch"] = int(base["resumption_watch"].fillna(False).sum())
    eligible = base[base["strength_tier"].notna()].copy()
    diagnostics.update({
        "atr20_coverage": atr_coverage, "near_high_profile": "board_60d",
        "separation_available": separation_available, "down_days_used": down_days_used,
        "rps_pool_count": len(rps_pool), "skipped_reason_counts": {key: value for key, value in sorted(skipped.items()) if value},
        "diagnostic_rows": base.loc[base["strength_tier"].isna(), ["sec_code", "first_failed_gate"] + [column for column in base.columns if column.startswith("gate_")]].to_dict("records"),
    })
    if eligible.empty:
        return StrongTrendEvaluation(pd.DataFrame(columns=_A_OUTPUT_COLUMNS), profile, diagnostics)

    weights = {"rps10_pct": 0.30, "rps20_pct": 0.30, "near_high_pct": 0.20}
    if separation_available:
        weights["separation_pct"] = 0.20
        eligible["ranking_profile"] = "benchmark"
    else:
        weights = {"rps10_pct": 0.375, "rps20_pct": 0.375, "near_high_pct": 0.25}
        eligible["ranking_profile"] = "no_benchmark"
    eligible["score"] = sum(eligible[column] * weight for column, weight in weights.items())
    eligible = eligible.sort_values(["score", "rps20_pct", "sec_code"], ascending=[False, False, True]).reset_index(drop=True)
    top_count = max(1, math.ceil(len(eligible) * 0.20))
    eligible["rank_band"] = "remainder"
    eligible.loc[: top_count - 1, "rank_band"] = "top20_pct"
    eligible["sec_name"] = eligible.get("sec_name", eligible["sec_code"]).fillna(eligible["sec_code"])
    diagnostics["eligible_count"] = len(eligible)
    diagnostics["result_count"] = len(eligible)
    evaluation = StrongTrendEvaluation(eligible.reindex(columns=_A_OUTPUT_COLUMNS), profile, diagnostics)
    context.evaluation_cache[cache_key] = evaluation
    return StrongTrendEvaluation(evaluation.rows.copy(), evaluation.trend_profile, copy.deepcopy(evaluation.diagnostics))


def _load_benchmark_returns(conn: sqlite3.Connection, dates: list[str], benchmark_code: str) -> pd.Series | None:
    if not dates:
        return None
    marks = ",".join("?" for _ in dates)
    index_bars = pd.read_sql_query(
        f"SELECT trade_date, close FROM ash.kline_daily WHERE sec_type='index' AND sec_code=? AND trade_date IN ({marks}) ORDER BY trade_date",
        conn, params=[str(benchmark_code), *dates],
    )
    if index_bars.empty:
        return None
    return pd.to_numeric(index_bars.set_index("trade_date")["close"], errors="coerce").reindex(dates).pct_change(fill_method=None)


def select_from_context(context: SelectionContext) -> list[Candidate]:
    evaluation = evaluate_strong_trend(context)
    return [
        Candidate(
            strategy_id=StrongTrendScanner.strategy_id,
            version=StrongTrendScanner.version,
            trade_date=context.trade_date,
            sec_type="stock", sec_code=str(row.sec_code).zfill(6), sec_name=str(row.sec_name),
            entry_price=float(row.reference_price),
            features={column: (None if pd.isna(getattr(row, column)) else getattr(row, column)) for column in evaluation.rows.columns if column not in {"sec_code", "sec_name", "reference_price"}},
            rank=rank,
        )
        for rank, row in enumerate(evaluation.rows.itertuples(index=False), start=1)
    ]


@register
class StrongTrendScanner(Scanner):
    strategy_id = "strong_trend"
    version = FORMAL_A_DEFINITION_VERSION
    kind = "stock"
    description = "V2.6 absolute strong-trend definition with continuation and fresh-breakout paths."
    default_params = {"benchmark_code": "000852", "min_down_days": 3}

    def run(self, conn: sqlite3.Connection, trade_date: str) -> list[Candidate]:
        context = build_selection_context(conn, trade_date, mode="close_final")
        self.last_universe_size = len(context.universe)
        return select_from_context(context)
