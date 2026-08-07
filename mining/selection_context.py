from __future__ import annotations

import sqlite3
import hashlib
import json
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, TypeVar

import pandas as pd

from .adjusted_prices import AdjustedPriceResult, build_forward_adjusted_bars
from .data_quality import usable_stock_trade_dates
from .universe import SelectionUniverseResult, build_selection_universe
from .trend_factors import resolve_trend_profile


SelectionResult = TypeVar("SelectionResult")
BENCHMARK_CODES = ("000852", "000300", "000001", "399001")


@dataclass
class SelectionContext:
    """Complete run inputs shared by every v2 selection capability."""

    bars: pd.DataFrame
    universe: pd.DataFrame
    trade_date: str
    mode: str
    as_of: str
    price_as_of: str
    metadata_as_of: str | None
    trend_profile: str | None
    data_status: str
    price_status: str = "ready"
    metadata_fresh: bool = True
    metadata_coverage: float | None = None
    metadata_provisional: bool = False
    benchmark_closes: dict[str, pd.Series] = field(default_factory=dict)
    benchmark_returns: pd.Series | None = None
    benchmark_status: str = "unavailable"
    activity_status: str = "unavailable"
    input_fingerprint: str | None = None
    diagnostics: dict[str, Any] = field(default_factory=dict)
    evaluation_cache: dict[str, Any] = field(default_factory=dict, repr=False)


def select_from_context(
    context: SelectionContext,
    selector: Callable[[SelectionContext], SelectionResult],
) -> SelectionResult:
    """Invoke a v2 selector from one complete, already-normalized input object."""
    return selector(context)


def _load_clean_bars(conn: sqlite3.Connection, clean_dates: list[str], sec_codes: list[str]) -> pd.DataFrame:
    if not clean_dates or not sec_codes:
        return pd.DataFrame()
    date_marks = ",".join("?" for _ in clean_dates)
    code_marks = ",".join("?" for _ in sec_codes)
    return pd.read_sql_query(
        f"""
        SELECT
          sec_code, trade_date, open, high, low, close, pre_close,
          change, change_pct, volume, amount, turnover_ratio
        FROM ash.kline_daily
        WHERE sec_type='stock'
          AND trade_date IN ({date_marks})
          AND sec_code IN ({code_marks})
        ORDER BY trade_date, sec_code
        """,
        conn,
        params=[*clean_dates, *sec_codes],
    )


def _load_benchmarks(
    conn: sqlite3.Connection,
    dates: list[str],
    *,
    snapshot_trade_date: str | None = None,
    snapshot_closes: Mapping[str, object] | None = None,
) -> tuple[dict[str, pd.Series], str]:
    if not dates or (snapshot_trade_date is not None and snapshot_closes is None):
        return {}, "unavailable"
    historical_dates = [str(day) for day in dates if str(day) != str(snapshot_trade_date)]
    if historical_dates:
        marks = ",".join("?" for _ in historical_dates)
        frame = pd.read_sql_query(
            f"""SELECT sec_code, trade_date, close FROM ash.kline_daily
                WHERE sec_type='index' AND sec_code IN ({','.join(repr(code) for code in BENCHMARK_CODES)})
                  AND trade_date IN ({marks})""",
            conn,
            params=historical_dates,
        )
    else:
        frame = pd.DataFrame(columns=["sec_code", "trade_date", "close"])
    closes = {
        str(code).zfill(6): pd.to_numeric(group.set_index("trade_date")["close"], errors="coerce").reindex(dates)
        for code, group in frame.groupby("sec_code")
    }
    if snapshot_trade_date is not None:
        current = {
            str(code).zfill(6): float(close)
            for code, close in snapshot_closes.items()
            if pd.notna(close) and pd.to_numeric(close, errors="coerce") > 0
        }
        if "000852" not in current:
            return {}, "unavailable"
        for code in set(closes).union(current):
            series = closes.get(code, pd.Series(index=dates, dtype="float64")).copy()
            if code in current:
                series.loc[str(snapshot_trade_date)] = current[code]
            closes[code] = series.reindex(dates)
    primary = closes.get("000852")
    return closes, "ready" if primary is not None and primary.notna().all() else "unavailable"


def _fingerprint(bars: pd.DataFrame, dates: list[str], benchmarks: dict[str, pd.Series]) -> str:
    canonical = bars.sort_values([column for column in ("trade_date", "sec_code") if column in bars.columns]).reset_index(drop=True)
    digest = hashlib.sha256()
    digest.update(pd.util.hash_pandas_object(canonical, index=False).values.tobytes())
    digest.update(json.dumps({"dates": dates, "benchmarks": {key: value.tolist() for key, value in sorted(benchmarks.items())}, "version": "v2.5"}, default=str, sort_keys=True).encode())
    return digest.hexdigest()


def _empty_context(
    universe_result: SelectionUniverseResult,
    *,
    mode: str,
    as_of: str,
    price_as_of: str,
    clean_dates: list[str],
) -> SelectionContext:
    diagnostics = {
        "clean_dates": clean_dates,
        "usable_dates": clean_dates,
        "universe": universe_result.diagnostics,
        "universe_count": universe_result.universe_count,
        "excluded_reason_counts": universe_result.excluded_reason_counts,
        "skipped_reason_counts": {},
    }
    return SelectionContext(
        bars=pd.DataFrame(),
        universe=universe_result.rows.copy(),
        trade_date=universe_result.trade_date,
        mode=mode,
        as_of=as_of,
        price_as_of=price_as_of,
        metadata_as_of=universe_result.metadata_as_of,
        trend_profile=(resolve_trend_profile(len(clean_dates)).profile_id if resolve_trend_profile(len(clean_dates)) else None),
        data_status=universe_result.data_status,
        price_status=universe_result.price_status,
        metadata_fresh=universe_result.metadata_fresh,
        metadata_coverage=universe_result.metadata_coverage,
        metadata_provisional=universe_result.metadata_provisional,
        diagnostics=diagnostics,
    )


def build_selection_context(
    conn: sqlite3.Connection,
    trade_date: str,
    *,
    mode: str = "close_final",
    as_of: str | None = None,
    price_as_of: str | None = None,
    snapshot_names: pd.DataFrame | Mapping[str, str] | None = None,
    clean_dates: list[str] | None = None,
    current_bars: pd.DataFrame | None = None,
    current_benchmark_closes: Mapping[str, object] | None = None,
    allow_missing_activity: bool = False,
) -> SelectionContext:
    """Build deterministic close-data input for v2 selectors without touching raw SQLite prices."""
    if clean_dates is None:
        resolved_clean_dates, quarantined_rows = usable_stock_trade_dates(
            conn,
            end_date=str(trade_date),
            include_end=current_bars is None,
        )
    else:
        resolved_clean_dates = sorted({str(day) for day in clean_dates if str(day) <= str(trade_date)})
        quarantined_rows = []
    context_dates = list(resolved_clean_dates)
    if current_bars is not None and str(trade_date) not in context_dates:
        context_dates.append(str(trade_date))
    resolved_as_of = str(as_of or trade_date)
    resolved_price_as_of = str(price_as_of or trade_date)
    universe_result = build_selection_universe(
        conn,
        str(trade_date),
        snapshot_names=snapshot_names,
        clean_dates=resolved_clean_dates,
        current_bars=current_bars,
        allow_missing_activity=allow_missing_activity,
    )
    if universe_result.rows.empty:
        return _empty_context(
            universe_result,
            mode=mode,
            as_of=resolved_as_of,
            price_as_of=resolved_price_as_of,
            clean_dates=context_dates,
        )

    sec_codes = universe_result.rows["sec_code"].astype(str).str.zfill(6).drop_duplicates().tolist()
    raw_bars = _load_clean_bars(conn, resolved_clean_dates, sec_codes)
    if current_bars is not None:
        current = pd.DataFrame(current_bars).copy()
        current["sec_code"] = current["sec_code"].astype(str).str.zfill(6)
        current = current[current["sec_code"].isin(sec_codes)].copy()
        current["trade_date"] = str(trade_date)
        for column in raw_bars.columns:
            if column not in current.columns:
                current[column] = pd.NA
        raw_bars = pd.concat([raw_bars, current[raw_bars.columns]], ignore_index=True)
    adjusted: AdjustedPriceResult = build_forward_adjusted_bars(
        raw_bars,
        allow_missing_activity=current_bars is not None and allow_missing_activity,
    )
    target_tradable = adjusted.bars["row_status"].eq("valid_trade")
    if current_bars is not None and allow_missing_activity:
        # Intraday snapshots can be price-valid before their activity fields are
        # available. ``build_forward_adjusted_bars`` has already verified the
        # price-only exception; provider halt placeholders never take this path.
        target_tradable = target_tradable | adjusted.bars["row_status"].eq("missing")
    target_tradable_codes = set(
        adjusted.bars.loc[
            (adjusted.bars["trade_date"].astype(str) == str(trade_date))
            & adjusted.bars["adjustment_valid"]
            & target_tradable,
            "sec_code",
        ].astype(str)
    )
    universe = universe_result.rows[universe_result.rows["sec_code"].isin(target_tradable_codes)].reset_index(drop=True)
    bars = adjusted.bars[
        adjusted.bars["adjustment_valid"] & adjusted.bars["sec_code"].isin(set(universe["sec_code"]))
    ].reset_index(drop=True)
    skipped = Counter(adjusted.skipped_reason_counts)
    skipped["invalid_adjusted_edges"] += sum(adjusted.skipped_reason_counts.values())
    diagnostics = {
        "clean_dates": context_dates,
        "usable_dates": resolved_clean_dates,
        "row_quarantine": quarantined_rows,
        "universe": universe_result.diagnostics,
        "universe_count": universe_result.universe_count,
        "eligible_count": len(universe),
        "excluded_reason_counts": universe_result.excluded_reason_counts,
        "adjustment_invalid_codes": adjusted.invalid_code_reasons,
        "adjustment_fallback_counts": adjusted.fallback_counts,
        "skipped_reason_counts": dict(sorted((key, value) for key, value in skipped.items() if value)),
    }
    benchmarks, benchmark_status = _load_benchmarks(
        conn,
        context_dates,
        snapshot_trade_date=str(trade_date) if current_bars is not None else None,
        snapshot_closes=current_benchmark_closes,
    )
    activity_available = universe.get("activity_available", pd.Series(dtype=bool))
    activity_status = "provisional" if current_bars is not None and activity_available.all() else "ready" if activity_available.all() else "unavailable"
    profile = resolve_trend_profile(len(context_dates))
    fingerprint = _fingerprint(bars, context_dates, benchmarks)
    diagnostics.update({"benchmark_status": benchmark_status, "activity_status": activity_status, "input_fingerprint": fingerprint})
    return SelectionContext(
        bars=bars,
        universe=universe,
        trade_date=str(trade_date),
        mode=str(mode),
        as_of=resolved_as_of,
        price_as_of=resolved_price_as_of,
        metadata_as_of=universe_result.metadata_as_of,
        trend_profile=profile.profile_id if profile else None,
        data_status=universe_result.data_status,
        price_status=universe_result.price_status,
        metadata_fresh=universe_result.metadata_fresh,
        metadata_coverage=universe_result.metadata_coverage,
        metadata_provisional=universe_result.metadata_provisional,
        benchmark_closes=benchmarks,
        benchmark_returns=(benchmarks.get("000852").pct_change(fill_method=None) if benchmark_status == "ready" else None),
        benchmark_status=benchmark_status,
        activity_status=activity_status,
        input_fingerprint=fingerprint,
        diagnostics=diagnostics,
    )
