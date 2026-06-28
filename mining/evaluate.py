from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from .backtest import backfill_outcomes
from .db import connect, create_strategy_run, list_stock_trade_dates, register_strategy, save_candidates
from .scanners import get_registered_scanners
from .scanners.momentum_breakout import (
    MomentumBreakoutScanner,
    select_candidates_from_universe as select_momentum_candidates,
)
from .scanners.rps_stock import (
    RpsStockScanner,
    select_candidates_from_universe as select_rps_candidates,
)
from .universe import build_universe

DEFAULT_LOOKBACK_BUFFER = 20
OUTCOME_HORIZON_DAYS = 5
DEFAULT_HIT_THRESHOLD = 0.05
EDGE_DISCOVERY_HIT_DELTA = 0.03
EDGE_TARGET_STRATEGIES = ("true_leader", "trend_embryo", "second_launch")


def resolve_eval_dates(
    conn: Any,
    start: str | None = None,
    end: str | None = None,
    last_n_days: int | None = None,
) -> list[str]:
    calendar = list_stock_trade_dates(conn)
    if not calendar:
        return []
    max_eval_index = len(calendar) - OUTCOME_HORIZON_DAYS - 1
    if max_eval_index < 0:
        return []

    min_eval_index = min(DEFAULT_LOOKBACK_BUFFER, max_eval_index)
    dates = calendar[min_eval_index : max_eval_index + 1]
    if end:
        dates = [day for day in dates if day <= end]
    if start:
        dates = [day for day in dates if day >= start]
    if last_n_days is not None and last_n_days > 0:
        dates = dates[-last_n_days:]
    return dates


def _scanner_ids(strategy_ids: Iterable[str] | None) -> set[str] | None:
    if strategy_ids is None:
        return None
    selected = {item.strip() for item in strategy_ids if item and item.strip()}
    return selected or None


def _has_existing_candidates(conn: Any, strategy_id: str, version: str, trade_date: str) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM candidates
        WHERE strategy_id=? AND version=? AND trade_date=?
        LIMIT 1
        """,
        (strategy_id, version, trade_date),
    ).fetchone()
    return row is not None


def run_scanners_for_dates(
    conn: Any,
    dates: list[str],
    strategy_ids: Iterable[str] | None = None,
    skip_existing: bool = True,
    progress_every: int = 10,
) -> dict[str, int]:
    selected_ids = _scanner_ids(strategy_ids)
    scanners = [scanner_cls() for scanner_cls in get_registered_scanners()]
    if selected_ids is not None:
        scanners = [scanner for scanner in scanners if scanner.strategy_id in selected_ids]

    runs = 0
    skipped = 0
    candidates_saved = 0
    for date_index, trade_date in enumerate(dates, start=1):
        stock_universe = None
        rps_stock_candidates = None
        rps_exclusion_codes: set[str] | None = None
        for scanner in scanners:
            register_strategy(conn, scanner)
            if skip_existing and _has_existing_candidates(conn, scanner.strategy_id, scanner.version, trade_date):
                skipped += 1
                continue

            if scanner.strategy_id == MomentumBreakoutScanner.strategy_id:
                if stock_universe is None:
                    stock_universe = build_universe(conn, trade_date)
                scanner.last_universe_size = len(stock_universe)
                if rps_stock_candidates is None:
                    rps_scanner = RpsStockScanner()
                    rps_stock_candidates = select_rps_candidates(
                        conn=conn,
                        trade_date=trade_date,
                        universe=stock_universe,
                        params=rps_scanner.params,
                        strategy_id=rps_scanner.strategy_id,
                        version=rps_scanner.version,
                    )
                    rps_exclusion_codes = {candidate.sec_code for candidate in rps_stock_candidates}
                candidates = select_momentum_candidates(
                    conn=conn,
                    trade_date=trade_date,
                    universe=stock_universe,
                    params=scanner.params,
                    strategy_id=scanner.strategy_id,
                    version=scanner.version,
                    rps_exclusion_codes=rps_exclusion_codes,
                )
            elif scanner.strategy_id == RpsStockScanner.strategy_id:
                if stock_universe is None:
                    stock_universe = build_universe(conn, trade_date)
                scanner.last_universe_size = len(stock_universe)
                if rps_stock_candidates is None:
                    rps_stock_candidates = select_rps_candidates(
                        conn=conn,
                        trade_date=trade_date,
                        universe=stock_universe,
                        params=scanner.params,
                        strategy_id=scanner.strategy_id,
                        version=scanner.version,
                    )
                candidates = rps_stock_candidates
            else:
                candidates = scanner.run(conn, trade_date)

            run_id = create_strategy_run(
                conn,
                scanner,
                trade_date=trade_date,
                status="ok" if candidates else "empty",
                n_candidates=len(candidates),
            )
            save_candidates(conn, run_id, candidates)
            runs += 1
            candidates_saved += len(candidates)
        if progress_every > 0 and (date_index % progress_every == 0 or date_index == len(dates)):
            print(
                f"processed {date_index}/{len(dates)} dates, runs={runs}, skipped={skipped}, candidates={candidates_saved}",
                flush=True,
            )
    return {"runs": runs, "skipped": skipped, "candidates_saved": candidates_saved}


def _current_registered_versions(strategy_ids: list[str] | None = None) -> dict[str, str]:
    wanted = set(strategy_ids or [])
    versions = {cls().strategy_id: cls().version for cls in get_registered_scanners()}
    return {key: value for key, value in versions.items() if not wanted or key in wanted}


def _load_eval_rows(conn: Any, start: str, end: str, strategies: list[str] | None = None) -> pd.DataFrame:
    params: list[Any] = [start, end]
    strategy_clause = ""
    active_versions = _current_registered_versions(strategies)
    if active_versions:
        terms: list[str] = []
        for strategy_id, version in active_versions.items():
            terms.append("(c.strategy_id=? AND c.version=?)")
            params.extend([strategy_id, version])
        strategy_clause = " AND (" + " OR ".join(terms) + ")"
    elif strategies:
        strategy_clause = f" AND c.strategy_id IN ({','.join('?' for _ in strategies)})"
        params.extend(strategies)
    return pd.read_sql_query(
        f"""
        SELECT
          c.strategy_id,
          c.trade_date,
          c.sec_code,
          c.rank,
          c.features_json,
          o.r4,
          o.r5,
          o.is_win
        FROM candidates c
        LEFT JOIN outcomes o ON o.candidate_id = c.candidate_id
        WHERE c.sec_type='stock'
          AND c.trade_date BETWEEN ? AND ?
          {strategy_clause}
        """,
        conn,
        params=params,
    )

def _score_from_features(value: Any) -> float:
    try:
        features = json.loads(value or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return math.nan
    try:
        return float(features.get("score", math.nan))
    except (TypeError, ValueError):
        return math.nan


def _features_frame(rows: pd.DataFrame) -> pd.DataFrame:
    if rows.empty or "features_json" not in rows.columns:
        return pd.DataFrame(index=rows.index)
    parsed = rows["features_json"].map(lambda value: json.loads(value or "{}"))
    features = pd.json_normalize(parsed)
    features.index = rows.index
    return features


def _bucket_label(series: pd.Series) -> pd.Series:
    scores = pd.to_numeric(series, errors="coerce")
    valid = scores.dropna()
    labels = pd.Series(pd.NA, index=series.index, dtype="object")
    if valid.empty or valid.nunique(dropna=True) < 2:
        return labels
    ranks = valid.rank(method="first", ascending=False)
    n = len(valid)
    high_cut = max(1, math.ceil(n / 3))
    low_cut = n - high_cut + 1
    labels.loc[valid.index[ranks <= high_cut]] = "high"
    labels.loc[valid.index[(ranks > high_cut) & (ranks < low_cut)]] = "mid"
    labels.loc[valid.index[ranks >= low_cut]] = "low"
    return labels


def _empty_summary() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            "strategy_id",
            "n_candidates",
            "n_evaluated",
            "win_rate",
            "avg_r4",
            "avg_r5",
            "median_r5",
            "std_r5",
            "mfe_avg",
            "mfe_median",
            "mae_median",
            "discovery_hit",
            "median_r5_vs_baseline",
            "win_rate_vs_baseline",
            "discovery_hit_vs_baseline",
            "score_high_avg_r5",
            "score_mid_avg_r5",
            "score_low_avg_r5",
            "score_monotonic",
            "topk_n",
            "topk_win_rate",
            "topk_avg_r5",
            "topk_median_r5",
            "topk_discovery_hit",
            "topk_median_r5_vs_baseline",
            "topk_win_rate_vs_baseline",
            "topk_discovery_hit_vs_baseline",
        ]
    )

def _empty_universe_baseline() -> dict[str, float | int | None]:
    return {
        "baseline_n": 0,
        "baseline_avg_r5": None,
        "baseline_median_r5": None,
        "baseline_win_rate": None,
        "baseline_mfe_avg": None,
        "baseline_mfe_median": None,
        "baseline_mae_median": None,
        "baseline_best_close_median": None,
        "baseline_discovery_hit": None,
    }


def _valid_eval_dates(calendar: list[str], start: str, end: str) -> list[str]:
    calendar_index = {day: idx for idx, day in enumerate(calendar)}
    return [
        day for day in calendar
        if start <= day <= end and calendar_index.get(day, -1) + OUTCOME_HORIZON_DAYS < len(calendar)
    ]


def _load_price_pivots_for_dates(conn: Any, dates: list[str], calendar: list[str]) -> dict[str, pd.DataFrame]:
    if not dates:
        return {"close": pd.DataFrame(), "high": pd.DataFrame(), "low": pd.DataFrame()}
    calendar_index = {day: idx for idx, day in enumerate(calendar)}
    needed_dates: set[str] = set()
    for day in dates:
        idx = calendar_index.get(day)
        if idx is None or idx + OUTCOME_HORIZON_DAYS >= len(calendar):
            continue
        needed_dates.update(calendar[idx : idx + OUTCOME_HORIZON_DAYS + 1])
    if not needed_dates:
        return {"close": pd.DataFrame(), "high": pd.DataFrame(), "low": pd.DataFrame()}
    raw = pd.read_sql_query(
        """
        SELECT sec_code, trade_date, close, high, low
        FROM ash.kline_daily
        WHERE sec_type='stock'
          AND trade_date BETWEEN ? AND ?
          AND close > 0
        """,
        conn,
        params=[min(needed_dates), max(needed_dates)],
    )
    if raw.empty:
        return {"close": pd.DataFrame(), "high": pd.DataFrame(), "low": pd.DataFrame()}
    raw["sec_code"] = raw["sec_code"].astype(str).str.zfill(6)
    return {
        "close": raw.pivot_table(index="trade_date", columns="sec_code", values="close", aggfunc="last").sort_index(),
        "high": raw.pivot_table(index="trade_date", columns="sec_code", values="high", aggfunc="last").sort_index(),
        "low": raw.pivot_table(index="trade_date", columns="sec_code", values="low", aggfunc="last").sort_index(),
    }


def _forward_metrics_from_pivots(
    pairs: pd.DataFrame,
    calendar: list[str],
    pivots: dict[str, pd.DataFrame],
    hit_threshold: float = DEFAULT_HIT_THRESHOLD,
) -> pd.DataFrame:
    columns = ["mfe", "mae", "best_close", "forward_r5", "forward_is_win", "discovery_hit"]
    metrics = pd.DataFrame(math.nan, index=pairs.index, columns=columns, dtype="float64")
    if pairs.empty or not calendar:
        return metrics
    close = pivots.get("close", pd.DataFrame())
    high = pivots.get("high", pd.DataFrame())
    low = pivots.get("low", pd.DataFrame())
    if close.empty or high.empty or low.empty:
        return metrics

    calendar_index = {day: idx for idx, day in enumerate(calendar)}
    normalized = pairs[["trade_date", "sec_code"]].copy()
    normalized["trade_date"] = normalized["trade_date"].astype(str)
    normalized["sec_code"] = normalized["sec_code"].astype(str).str.zfill(6)
    for day, index_labels in normalized.groupby("trade_date", sort=False).groups.items():
        idx = calendar_index.get(day)
        if idx is None or idx + OUTCOME_HORIZON_DAYS >= len(calendar):
            continue
        window_dates = calendar[idx + 1 : idx + OUTCOME_HORIZON_DAYS + 1]
        t1 = window_dates[0]
        t5 = window_dates[-1]
        if day not in close.index or any(d not in close.index for d in window_dates):
            continue
        if any(d not in high.index for d in window_dates) or any(d not in low.index for d in window_dates):
            continue

        codes = normalized.loc[index_labels, "sec_code"]
        unique_codes = sorted(set(codes.tolist()))
        entry = close.loc[day].reindex(unique_codes)
        future_high = high.reindex(window_dates).reindex(columns=unique_codes)
        future_low = low.reindex(window_dates).reindex(columns=unique_codes)
        future_close = close.reindex(window_dates).reindex(columns=unique_codes)
        high_t1 = high.loc[t1].reindex(unique_codes)
        low_t1 = low.loc[t1].reindex(unique_codes)
        close_t5 = close.loc[t5].reindex(unique_codes)
        complete = (
            entry.notna()
            & close_t5.notna()
            & high_t1.notna()
            & low_t1.notna()
            & future_high.notna().all(axis=0)
            & future_low.notna().all(axis=0)
            & future_close.notna().all(axis=0)
        )
        if not complete.any():
            continue

        by_code = pd.DataFrame(index=pd.Index(unique_codes, name="sec_code"), columns=columns, dtype="float64")
        by_code.loc[complete, "mfe"] = future_high.loc[:, complete].max(axis=0) / entry.loc[complete] - 1.0
        by_code.loc[complete, "mae"] = future_low.loc[:, complete].min(axis=0) / entry.loc[complete] - 1.0
        by_code.loc[complete, "best_close"] = future_close.loc[:, complete].max(axis=0) / entry.loc[complete] - 1.0
        by_code.loc[complete, "forward_r5"] = close_t5.loc[complete] / entry.loc[complete] - 1.0
        by_code.loc[complete, "forward_is_win"] = (
            ((high_t1.loc[complete] / entry.loc[complete] - 1.0) > 0.02)
            & ((low_t1.loc[complete] / entry.loc[complete] - 1.0) > -0.03)
        ).astype(float)
        by_code.loc[complete, "discovery_hit"] = (by_code.loc[complete, "mfe"] >= hit_threshold).astype(float)
        for column in columns:
            metrics.loc[index_labels, column] = codes.map(by_code[column]).to_numpy(dtype="float64")
    return metrics


def compute_forward_window_metrics(
    conn: Any,
    pairs: pd.DataFrame,
    hit_threshold: float = DEFAULT_HIT_THRESHOLD,
) -> pd.DataFrame:
    if pairs.empty:
        return _forward_metrics_from_pivots(pairs, [], {}, hit_threshold=hit_threshold)
    calendar = list_stock_trade_dates(conn)
    calendar_set = set(calendar)
    dates = sorted(set(pairs["trade_date"].astype(str).tolist()))
    valid_dates = [day for day in dates if day in calendar_set]
    pivots = _load_price_pivots_for_dates(conn, valid_dates, calendar)
    return _forward_metrics_from_pivots(pairs, calendar, pivots, hit_threshold=hit_threshold)


def _attach_forward_metrics(conn: Any, rows: pd.DataFrame, hit_threshold: float) -> pd.DataFrame:
    result = rows.copy()
    if result.empty:
        return result
    metrics = compute_forward_window_metrics(conn, result[["trade_date", "sec_code"]], hit_threshold=hit_threshold)
    for column in metrics.columns:
        result[column] = metrics[column]
    return result


def _baseline_from_forward_metrics(metrics: pd.DataFrame) -> dict[str, float | int | None]:
    complete = metrics[
        pd.to_numeric(metrics["forward_r5"], errors="coerce").notna()
        & pd.to_numeric(metrics["forward_is_win"], errors="coerce").notna()
        & pd.to_numeric(metrics["discovery_hit"], errors="coerce").notna()
    ].copy()
    if complete.empty:
        return _empty_universe_baseline()
    return {
        "baseline_n": int(len(complete)),
        "baseline_avg_r5": float(complete["forward_r5"].mean()),
        "baseline_median_r5": float(complete["forward_r5"].median()),
        "baseline_win_rate": float(complete["forward_is_win"].mean()),
        "baseline_mfe_avg": float(complete["mfe"].mean()),
        "baseline_mfe_median": float(complete["mfe"].median()),
        "baseline_mae_median": float(complete["mae"].median()),
        "baseline_best_close_median": float(complete["best_close"].median()),
        "baseline_discovery_hit": float(complete["discovery_hit"].mean()),
    }
def aggregate_results(
    conn: Any,
    start: str,
    end: str,
    strategies: list[str] | None = None,
    baseline: dict[str, float | int | None] | None = None,
    top_k: int = 5,
    hit_threshold: float = DEFAULT_HIT_THRESHOLD,
) -> pd.DataFrame:
    rows = _load_eval_rows(conn, start, end, strategies)
    if rows.empty:
        return _empty_summary()

    rows["score"] = rows["features_json"].map(_score_from_features)
    rows = _attach_forward_metrics(conn, rows, hit_threshold=hit_threshold)
    complete = rows[
        pd.to_numeric(rows["r4"], errors="coerce").notna()
        & pd.to_numeric(rows["r5"], errors="coerce").notna()
        & pd.to_numeric(rows["discovery_hit"], errors="coerce").notna()
    ].copy()
    candidate_counts = rows.groupby("strategy_id").size().rename("n_candidates")
    if complete.empty:
        result = candidate_counts.reset_index()
        result["n_evaluated"] = 0
        for column in [
            "win_rate",
            "avg_r4",
            "avg_r5",
            "median_r5",
            "std_r5",
            "mfe_avg",
            "mfe_median",
            "mae_median",
            "discovery_hit",
            "median_r5_vs_baseline",
            "win_rate_vs_baseline",
            "discovery_hit_vs_baseline",
            "score_high_avg_r5",
            "score_mid_avg_r5",
            "score_low_avg_r5",
            "topk_win_rate",
            "topk_avg_r5",
            "topk_median_r5",
            "topk_discovery_hit",
            "topk_median_r5_vs_baseline",
            "topk_win_rate_vs_baseline",
            "topk_discovery_hit_vs_baseline",
        ]:
            result[column] = math.nan
        result["score_monotonic"] = pd.NA
        result["topk_n"] = 0
        return result

    complete["is_win"] = pd.to_numeric(complete["is_win"], errors="coerce")
    grouped = complete.groupby("strategy_id")
    summary = grouped.agg(
        n_evaluated=("sec_code", "size"),
        win_rate=("is_win", "mean"),
        avg_r4=("r4", "mean"),
        avg_r5=("r5", "mean"),
        median_r5=("r5", "median"),
        std_r5=("r5", "std"),
        mfe_avg=("mfe", "mean"),
        mfe_median=("mfe", "median"),
        mae_median=("mae", "median"),
        discovery_hit=("discovery_hit", "mean"),
    ).reset_index()
    summary = summary.merge(candidate_counts.reset_index(), on="strategy_id", how="left")

    bucket_rows: list[dict[str, Any]] = []
    topk_rows: list[dict[str, Any]] = []
    for strategy_id, frame in complete.groupby("strategy_id"):
        frame = frame.copy()
        frame["score_bucket"] = _bucket_label(frame["score"])
        bucket_avg = frame.groupby("score_bucket", dropna=True, observed=False)["r5"].mean().to_dict()
        high = bucket_avg.get("high", math.nan)
        mid = bucket_avg.get("mid", math.nan)
        low = bucket_avg.get("low", math.nan)
        monotonic = None
        if all(pd.notna(value) for value in [high, mid, low]):
            monotonic = bool(high >= mid >= low)
        bucket_rows.append(
            {
                "strategy_id": strategy_id,
                "score_high_avg_r5": high,
                "score_mid_avg_r5": mid,
                "score_low_avg_r5": low,
                "score_monotonic": monotonic,
            }
        )

        topk = (
            frame.sort_values(["trade_date", "score", "rank", "sec_code"], ascending=[True, False, True, True])
            .groupby("trade_date", group_keys=False)
            .head(top_k)
        )
        topk_rows.append(
            {
                "strategy_id": strategy_id,
                "topk_n": int(len(topk)),
                "topk_win_rate": float(topk["is_win"].mean()) if not topk.empty else math.nan,
                "topk_avg_r5": float(topk["r5"].mean()) if not topk.empty else math.nan,
                "topk_median_r5": float(topk["r5"].median()) if not topk.empty else math.nan,
                "topk_discovery_hit": float(topk["discovery_hit"].mean()) if not topk.empty else math.nan,
            }
        )
    summary = summary.merge(pd.DataFrame(bucket_rows), on="strategy_id", how="left")
    summary = summary.merge(pd.DataFrame(topk_rows), on="strategy_id", how="left")

    baseline = baseline or {}
    baseline_median = baseline.get("baseline_median_r5")
    baseline_win = baseline.get("baseline_win_rate")
    baseline_discovery = baseline.get("baseline_discovery_hit")
    summary["median_r5_vs_baseline"] = summary["median_r5"] - baseline_median if baseline_median is not None else math.nan
    summary["win_rate_vs_baseline"] = summary["win_rate"] - baseline_win if baseline_win is not None else math.nan
    summary["discovery_hit_vs_baseline"] = summary["discovery_hit"] - baseline_discovery if baseline_discovery is not None else math.nan
    summary["topk_median_r5_vs_baseline"] = summary["topk_median_r5"] - baseline_median if baseline_median is not None else math.nan
    summary["topk_win_rate_vs_baseline"] = summary["topk_win_rate"] - baseline_win if baseline_win is not None else math.nan
    summary["topk_discovery_hit_vs_baseline"] = summary["topk_discovery_hit"] - baseline_discovery if baseline_discovery is not None else math.nan
    return summary[
        [
            "strategy_id",
            "n_candidates",
            "n_evaluated",
            "win_rate",
            "avg_r4",
            "avg_r5",
            "median_r5",
            "std_r5",
            "mfe_avg",
            "mfe_median",
            "mae_median",
            "discovery_hit",
            "median_r5_vs_baseline",
            "win_rate_vs_baseline",
            "discovery_hit_vs_baseline",
            "score_high_avg_r5",
            "score_mid_avg_r5",
            "score_low_avg_r5",
            "score_monotonic",
            "topk_n",
            "topk_win_rate",
            "topk_avg_r5",
            "topk_median_r5",
            "topk_discovery_hit",
            "topk_median_r5_vs_baseline",
            "topk_win_rate_vs_baseline",
            "topk_discovery_hit_vs_baseline",
        ]
    ].sort_values("strategy_id").reset_index(drop=True)

def compute_baseline_daily_sql(conn: Any, start: str, end: str) -> dict[str, float | int | None]:
    dates = [day for day in list_stock_trade_dates(conn) if start <= day <= end]
    calendar = list_stock_trade_dates(conn)
    calendar_index = {day: idx for idx, day in enumerate(calendar)}
    returns: list[float] = []
    wins: list[int] = []
    for trade_date in dates:
        idx = calendar_index.get(trade_date)
        if idx is None or idx + OUTCOME_HORIZON_DAYS >= len(calendar):
            continue
        t1 = calendar[idx + 1]
        t5 = calendar[idx + OUTCOME_HORIZON_DAYS]
        current = pd.read_sql_query(
            """
            SELECT sec_code, close AS entry_close
            FROM ash.kline_daily
            WHERE sec_type='stock' AND trade_date=? AND close > 0
            """,
            conn,
            params=[trade_date],
        )
        t1_frame = pd.read_sql_query(
            """
            SELECT sec_code, high AS high_t1, low AS low_t1
            FROM ash.kline_daily
            WHERE sec_type='stock' AND trade_date=?
            """,
            conn,
            params=[t1],
        )
        future = pd.read_sql_query(
            """
            SELECT sec_code, close AS close_t5
            FROM ash.kline_daily
            WHERE sec_type='stock' AND trade_date=? AND close > 0
            """,
            conn,
            params=[t5],
        )
        merged = current.merge(t1_frame, on="sec_code", how="inner").merge(future, on="sec_code", how="inner")
        if merged.empty:
            continue
        returns.extend(((merged["close_t5"] / merged["entry_close"]) - 1.0).dropna().astype(float).tolist())
        wins.extend(
            (
                ((merged["high_t1"] / merged["entry_close"] - 1.0) > 0.02)
                & ((merged["low_t1"] / merged["entry_close"] - 1.0) > -0.03)
            )
            .astype(int)
            .tolist()
        )
    if not returns:
        return {"baseline_n": 0, "baseline_avg_r5": None, "baseline_median_r5": None, "baseline_win_rate": None}
    series = pd.Series(returns, dtype="float64")
    win_series = pd.Series(wins, dtype="float64")
    return {
        "baseline_n": int(series.size),
        "baseline_avg_r5": float(series.mean()),
        "baseline_median_r5": float(series.median()),
        "baseline_win_rate": float(win_series.mean()),
    }


def compute_baseline(conn: Any, start: str, end: str) -> dict[str, float | int | None]:
    calendar = list_stock_trade_dates(conn)
    eval_dates = [day for day in calendar if start <= day <= end]
    calendar_index = {day: idx for idx, day in enumerate(calendar)}
    valid_dates = [day for day in eval_dates if calendar_index.get(day, -1) + OUTCOME_HORIZON_DAYS < len(calendar)]
    if not valid_dates:
        return {"baseline_n": 0, "baseline_avg_r5": None, "baseline_median_r5": None, "baseline_win_rate": None}

    needed_dates = set(valid_dates)
    for day in valid_dates:
        idx = calendar_index[day]
        needed_dates.add(calendar[idx + 1])
        needed_dates.add(calendar[idx + OUTCOME_HORIZON_DAYS])
    min_date = min(needed_dates)
    max_date = max(needed_dates)
    raw = pd.read_sql_query(
        """
        SELECT sec_code, trade_date, close, high, low
        FROM ash.kline_daily
        WHERE sec_type='stock'
          AND trade_date BETWEEN ? AND ?
          AND close > 0
        """,
        conn,
        params=[min_date, max_date],
    )
    if raw.empty:
        return {"baseline_n": 0, "baseline_avg_r5": None, "baseline_median_r5": None, "baseline_win_rate": None}
    close = raw.pivot_table(index="trade_date", columns="sec_code", values="close", aggfunc="last").sort_index()
    high = raw.pivot_table(index="trade_date", columns="sec_code", values="high", aggfunc="last").sort_index()
    low = raw.pivot_table(index="trade_date", columns="sec_code", values="low", aggfunc="last").sort_index()

    returns: list[pd.Series] = []
    wins: list[pd.Series] = []
    for day in valid_dates:
        idx = calendar_index[day]
        t1 = calendar[idx + 1]
        t5 = calendar[idx + OUTCOME_HORIZON_DAYS]
        if day not in close.index or t1 not in high.index or t1 not in low.index or t5 not in close.index:
            continue
        entry = close.loc[day]
        ret = (close.loc[t5] / entry - 1.0).replace([math.inf, -math.inf], math.nan).dropna()
        win = (((high.loc[t1] / entry - 1.0) > 0.02) & ((low.loc[t1] / entry - 1.0) > -0.03)).reindex(ret.index).dropna()
        returns.append(ret)
        wins.append(win.astype(int))
    if not returns:
        return {"baseline_n": 0, "baseline_avg_r5": None, "baseline_median_r5": None, "baseline_win_rate": None}
    ret_series = pd.concat(returns).astype("float64")
    win_series = pd.concat(wins).astype("float64")
    return {
        "baseline_n": int(ret_series.size),
        "baseline_avg_r5": float(ret_series.mean()),
        "baseline_median_r5": float(ret_series.median()),
        "baseline_win_rate": float(win_series.mean()),
    }


def compute_baseline_universe(
    conn: Any,
    start: str,
    end: str,
    hit_threshold: float = DEFAULT_HIT_THRESHOLD,
) -> dict[str, float | int | None]:
    calendar = list_stock_trade_dates(conn)
    valid_dates = _valid_eval_dates(calendar, start, end)
    if not valid_dates:
        return _empty_universe_baseline()

    pivots = _load_price_pivots_for_dates(conn, valid_dates, calendar)
    pair_frames: list[pd.DataFrame] = []
    for trade_date in valid_dates:
        universe = build_universe(conn, trade_date)
        if universe.empty or "sec_code" not in universe.columns:
            continue
        codes = universe["sec_code"].astype(str).str.zfill(6).dropna().drop_duplicates()
        if codes.empty:
            continue
        pair_frames.append(pd.DataFrame({"trade_date": trade_date, "sec_code": codes.to_list()}))
    if not pair_frames:
        return _empty_universe_baseline()

    pairs = pd.concat(pair_frames, ignore_index=True)
    metrics = _forward_metrics_from_pivots(pairs, calendar, pivots, hit_threshold=hit_threshold)
    return _baseline_from_forward_metrics(metrics)
def diagnose_trend_embryo(conn: Any, start: str, end: str) -> dict[str, pd.DataFrame]:
    rows = _load_eval_rows(conn, start, end, ["trend_embryo"])
    complete = rows[
        pd.to_numeric(rows["r4"], errors="coerce").notna()
        & pd.to_numeric(rows["r5"], errors="coerce").notna()
    ].copy()
    if complete.empty:
        return {}
    features = _features_frame(complete)
    data = pd.concat([complete.drop(columns=["features_json"]), features], axis=1)
    result: dict[str, pd.DataFrame] = {}
    if "limit_up_count_5d" in data.columns:
        result["limit_up_count_5d"] = data.groupby("limit_up_count_5d", dropna=False)["r5"].agg(["count", "mean", "median"]).reset_index()
    if "small_yang_count" in data.columns:
        result["small_yang_count"] = data.groupby("small_yang_count", dropna=False)["r5"].agg(["count", "mean", "median"]).reset_index()
    if "single_day_max_change" in data.columns:
        data["single_day_max_change_bucket"] = pd.cut(
            pd.to_numeric(data["single_day_max_change"], errors="coerce"),
            [-1e9, 5, 8, 10, 12, 1e9],
            labels=["<=5", "5-8", "8-10", "10-12", ">12"],
        )
        result["single_day_max_change"] = data.groupby("single_day_max_change_bucket", observed=False)["r5"].agg(["count", "mean", "median"]).reset_index()
    if "ret_5d" in data.columns:
        data["ret_5d_bucket"] = pd.cut(
            pd.to_numeric(data["ret_5d"], errors="coerce"),
            [-1e9, 18, 25, 32, 40, 1e9],
            labels=["<=18", "18-25", "25-32", "32-40", ">40"],
        )
        result["ret_5d"] = data.groupby("ret_5d_bucket", observed=False)["r5"].agg(["count", "mean", "median"]).reset_index()
    return result


def classify_edge_verdict(row: pd.Series) -> str:
    median_ok = bool(pd.notna(row.get("topk_median_r5_vs_baseline")) and row.get("topk_median_r5_vs_baseline") > 0)
    discovery_ok = bool(
        pd.notna(row.get("topk_discovery_hit_vs_baseline"))
        and row.get("topk_discovery_hit_vs_baseline") > EDGE_DISCOVERY_HIT_DELTA
    )
    win_ok = bool(pd.notna(row.get("topk_win_rate_vs_baseline")) and row.get("topk_win_rate_vs_baseline") > 0)
    if median_ok and discovery_ok and win_ok:
        return "has_edge"
    if discovery_ok and not median_ok:
        return "discovery_only"
    return "no_edge"


def edge_verdicts(summary: pd.DataFrame, strategies: Iterable[str] = EDGE_TARGET_STRATEGIES) -> pd.DataFrame:
    columns = [
        "strategy_id",
        "edge_verdict",
        "topk_median_r5_vs_baseline",
        "topk_discovery_hit_vs_baseline",
        "topk_win_rate_vs_baseline",
    ]
    if summary.empty:
        return pd.DataFrame(columns=columns)
    rows = summary[summary["strategy_id"].isin(set(strategies))].copy()
    if rows.empty:
        return pd.DataFrame(columns=columns)
    rows["edge_verdict"] = rows.apply(classify_edge_verdict, axis=1)
    return rows[columns].sort_values("strategy_id").reset_index(drop=True)
def _format_pct(value: Any) -> str:
    if value is None or pd.isna(value):
        return "-"
    return f"{float(value) * 100:.2f}%"


def _format_baseline(prefix: str, baseline: dict[str, float | int | None]) -> str:
    parts = [
        f"{prefix} n={baseline.get('baseline_n', 0)}",
        f"avg_r5={_format_pct(baseline.get('baseline_avg_r5'))}",
        f"median_r5={_format_pct(baseline.get('baseline_median_r5'))}",
        f"win_rate={_format_pct(baseline.get('baseline_win_rate'))}",
    ]
    if "baseline_discovery_hit" in baseline:
        parts.extend(
            [
                f"mfe_median={_format_pct(baseline.get('baseline_mfe_median'))}",
                f"mae_median={_format_pct(baseline.get('baseline_mae_median'))}",
                f"discovery_hit={_format_pct(baseline.get('baseline_discovery_hit'))}",
            ]
        )
    return " ".join(parts)


def write_markdown_report(
    summary: pd.DataFrame,
    baseline: dict[str, float | int | None],
    start: str,
    end: str,
    out_dir: str | Path = "output",
    top_k: int = 5,
    baseline_market: dict[str, float | int | None] | None = None,
    hit_threshold: float = DEFAULT_HIT_THRESHOLD,
) -> str:
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    path = out_path / f"scanner_eval_{start}_{end}.md"
    baseline_universe = baseline
    baseline_market = baseline_market or baseline
    lines = [
        f"# Scanner Evaluation {start} -> {end}",
        "",
        "Note: avg_r5 can be distorted by fat tails; decision deltas use baseline_universe, not baseline_market.",
        "",
        _format_baseline("baseline_market (reference)", baseline_market),
        _format_baseline("baseline_universe (decision)", baseline_universe),
        f"Discovery hit threshold: {_format_pct(hit_threshold)} MFE within t+1..t+5",
        f"Top-k per day: {top_k}",
        "",
    ]
    verdict_table = edge_verdicts(summary)
    if not verdict_table.empty:
        lines.extend(["## Edge Verdicts", ""])
        verdict_display = verdict_table.copy()
        for column in ["topk_median_r5_vs_baseline", "topk_discovery_hit_vs_baseline", "topk_win_rate_vs_baseline"]:
            verdict_display[column] = verdict_display[column].map(_format_pct)
        lines.extend([verdict_display.to_markdown(index=False), ""])
    if summary.empty:
        lines.append("No complete outcomes in range.")
    else:
        table = summary.copy()
        baseline_avg = baseline_universe.get("baseline_avg_r5")
        table["avg_r5_vs_baseline"] = table["avg_r5"] - baseline_avg if baseline_avg is not None else math.nan
        for column in [
            "win_rate",
            "avg_r4",
            "avg_r5",
            "median_r5",
            "std_r5",
            "mfe_avg",
            "mfe_median",
            "mae_median",
            "discovery_hit",
            "median_r5_vs_baseline",
            "win_rate_vs_baseline",
            "discovery_hit_vs_baseline",
            "score_high_avg_r5",
            "score_mid_avg_r5",
            "score_low_avg_r5",
            "topk_win_rate",
            "topk_avg_r5",
            "topk_median_r5",
            "topk_discovery_hit",
            "topk_median_r5_vs_baseline",
            "topk_win_rate_vs_baseline",
            "topk_discovery_hit_vs_baseline",
            "avg_r5_vs_baseline",
        ]:
            if column in table.columns:
                table[column] = table[column].map(_format_pct)
        lines.append(table.to_markdown(index=False))
    path.write_text("\n".join(lines), encoding="utf-8")
    return str(path)

def write_diagnostics_report(
    diagnostics: dict[str, pd.DataFrame],
    start: str,
    end: str,
    out_dir: str | Path = "output",
) -> str | None:
    if not diagnostics:
        return None
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    path = out_path / f"trend_embryo_diagnostics_{start}_{end}.md"
    lines = [f"# Trend Embryo Diagnostics {start} -> {end}", ""]
    for name, frame in diagnostics.items():
        lines.extend([f"## {name}", frame.to_markdown(index=False), ""])
    path.write_text("\n".join(lines), encoding="utf-8")
    return str(path)


def evaluate_range(
    base_dir: str | Path = ".",
    start: str | None = None,
    end: str | None = None,
    last_n_days: int | None = None,
    strategies: list[str] | None = None,
    out_dir: str | Path = "output",
    rerun: bool = True,
    skip_existing: bool = True,
    progress_every: int = 10,
    top_k: int = 5,
    hit_threshold: float = DEFAULT_HIT_THRESHOLD,
    diagnose: str | None = None,
) -> dict[str, Any]:
    conn = connect(base_dir=base_dir)
    try:
        dates = resolve_eval_dates(conn, start=start, end=end, last_n_days=last_n_days)
        if not dates:
            summary = aggregate_results(conn, start or "", end or "", strategies, top_k=top_k, hit_threshold=hit_threshold)
            baseline_market = {"baseline_n": 0, "baseline_avg_r5": None, "baseline_median_r5": None, "baseline_win_rate": None}
            baseline_universe = _empty_universe_baseline()
            return {
                "start": start,
                "end": end,
                "dates": [],
                "summary": summary,
                "baseline": baseline_universe,
                "baseline_market": baseline_market,
                "baseline_universe": baseline_universe,
                "edge_verdicts": edge_verdicts(summary),
                "report": None,
                "diagnostics": {},
            }
        eval_start = dates[0]
        eval_end = dates[-1]
        run_result = {"runs": 0, "skipped": 0, "candidates_saved": 0}
        if rerun:
            run_result = run_scanners_for_dates(
                conn,
                dates,
                strategy_ids=strategies,
                skip_existing=skip_existing,
                progress_every=progress_every,
            )
            backfill_outcomes(conn, force=False)
        baseline_market = compute_baseline(conn, eval_start, eval_end)
        baseline_universe = compute_baseline_universe(conn, eval_start, eval_end, hit_threshold=hit_threshold)
        summary = aggregate_results(
            conn,
            eval_start,
            eval_end,
            strategies,
            baseline=baseline_universe,
            top_k=top_k,
            hit_threshold=hit_threshold,
        )
        verdicts = edge_verdicts(summary)
        report = write_markdown_report(
            summary,
            baseline_universe,
            eval_start,
            eval_end,
            out_dir=out_dir,
            top_k=top_k,
            baseline_market=baseline_market,
            hit_threshold=hit_threshold,
        )
        diagnostics: dict[str, pd.DataFrame] = {}
        diagnostic_report = None
        if diagnose == "trend_embryo":
            diagnostics = diagnose_trend_embryo(conn, eval_start, eval_end)
            diagnostic_report = write_diagnostics_report(diagnostics, eval_start, eval_end, out_dir=out_dir)
        return {
            "start": eval_start,
            "end": eval_end,
            "dates": dates,
            "run": run_result,
            "summary": summary,
            "baseline": baseline_universe,
            "baseline_market": baseline_market,
            "baseline_universe": baseline_universe,
            "edge_verdicts": verdicts,
            "report": report,
            "diagnostics": diagnostics,
            "diagnostic_report": diagnostic_report,
        }
    finally:
        conn.close()

def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate mining scanners against completed outcomes.")
    parser.add_argument("--base-dir", default=Path.cwd())
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--last-n-days", type=int)
    parser.add_argument("--strategies", nargs="*")
    parser.add_argument("--out-dir", default="output")
    parser.add_argument("--no-rerun", action="store_true")
    parser.add_argument("--skip-existing", dest="skip_existing", action="store_true", default=True)
    parser.add_argument("--no-skip-existing", dest="skip_existing", action="store_false")
    parser.add_argument("--progress-every", type=int, default=10)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--hit-threshold", type=float, default=DEFAULT_HIT_THRESHOLD)
    parser.add_argument("--diagnose", choices=["trend_embryo"])
    args = parser.parse_args()

    result = evaluate_range(
        base_dir=args.base_dir,
        start=args.start,
        end=args.end,
        last_n_days=args.last_n_days,
        strategies=args.strategies,
        out_dir=args.out_dir,
        rerun=not args.no_rerun,
        skip_existing=args.skip_existing,
        progress_every=args.progress_every,
        top_k=args.top_k,
        hit_threshold=args.hit_threshold,
        diagnose=args.diagnose,
    )
    print(f"evaluated {len(result['dates'])} dates: {result['start']} -> {result['end']}")
    print(result["summary"].to_string(index=False) if not result["summary"].empty else "no complete outcomes")
    print(f"baseline_market={result['baseline_market']}")
    print(f"baseline_universe={result['baseline_universe']}")
    if not result["edge_verdicts"].empty:
        print("edge_verdicts=")
        print(result["edge_verdicts"].to_string(index=False))
    print(f"report={result['report']}")
    if result.get("diagnostic_report"):
        print(f"diagnostic_report={result['diagnostic_report']}")


if __name__ == "__main__":
    main()