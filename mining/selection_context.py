from __future__ import annotations

import sqlite3
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, TypeVar

import pandas as pd

from .adjusted_prices import AdjustedPriceResult, build_forward_adjusted_bars
from .data_quality import clean_stock_trade_dates
from .universe import SelectionUniverseResult, build_selection_universe


SelectionResult = TypeVar("SelectionResult")


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
    diagnostics: dict[str, Any] = field(default_factory=dict)


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
        trend_profile=None,
        data_status=universe_result.data_status,
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
    allow_missing_activity: bool = False,
) -> SelectionContext:
    """Build deterministic close-data input for v2 selectors without touching raw SQLite prices."""
    resolved_clean_dates = (
        clean_stock_trade_dates(conn, end_date=str(trade_date), include_end=current_bars is None)
        if clean_dates is None
        else sorted({str(day) for day in clean_dates if str(day) <= str(trade_date)})
    )
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
            clean_dates=resolved_clean_dates,
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
    adjusted: AdjustedPriceResult = build_forward_adjusted_bars(raw_bars)
    valid_codes = adjusted.bars.loc[adjusted.bars["adjustment_valid"], "sec_code"].drop_duplicates()
    universe = universe_result.rows[universe_result.rows["sec_code"].isin(valid_codes)].reset_index(drop=True)
    bars = adjusted.bars[adjusted.bars["sec_code"].isin(set(universe["sec_code"]))].reset_index(drop=True)
    skipped = Counter(adjusted.skipped_reason_counts)
    skipped["invalid_adjusted_series"] += len(adjusted.invalid_code_reasons)
    diagnostics = {
        "clean_dates": resolved_clean_dates,
        "universe": universe_result.diagnostics,
        "universe_count": universe_result.universe_count,
        "eligible_count": len(universe),
        "excluded_reason_counts": universe_result.excluded_reason_counts,
        "adjustment_invalid_codes": adjusted.invalid_code_reasons,
        "adjustment_fallback_counts": adjusted.fallback_counts,
        "skipped_reason_counts": dict(sorted((key, value) for key, value in skipped.items() if value)),
    }
    return SelectionContext(
        bars=bars,
        universe=universe,
        trade_date=str(trade_date),
        mode=str(mode),
        as_of=resolved_as_of,
        price_as_of=resolved_price_as_of,
        metadata_as_of=universe_result.metadata_as_of,
        trend_profile=None,
        data_status=universe_result.data_status,
        diagnostics=diagnostics,
    )
