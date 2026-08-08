from __future__ import annotations

import math
import sqlite3
from collections import Counter
from typing import Any

import pandas as pd

from ..event_activity import build_event_activity
from ..capabilities import SCREENING_DEFINITION_VERSION
from ..selection_context import SelectionContext, build_selection_context
from ..trend_factors import resolve_trend_profile
from . import Candidate, Scanner, register


def evaluate_base_breakout(context: SelectionContext) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Formal D capability: a T-confirmed breakout from a T-1-defined medium base."""
    clean_dates = [str(value) for value in context.diagnostics.get("clean_dates", [])]
    dates = list(clean_dates)
    if str(context.trade_date) not in dates:
        dates.append(str(context.trade_date))
    profile = resolve_trend_profile(len(clean_dates))
    diagnostics: dict[str, Any] = {
        "trend_profile": profile.profile_id if profile else None,
        "result_count": 0,
        "skipped_reason_counts": {},
    }
    if getattr(context, "price_status", context.data_status) == "unavailable" or profile is None or context.bars.empty:
        diagnostics["skipped_reason_counts"] = {
            "insufficient_history" if profile is None else "data_unavailable": len(context.universe)
        }
        return pd.DataFrame(), diagnostics
    required = max(61, profile.long_window + 1)
    if len(dates) < required:
        diagnostics["skipped_reason_counts"] = {"insufficient_clean_history": len(context.universe)}
        return pd.DataFrame(), diagnostics
    activity = build_event_activity(context)
    activity_by_code = activity.rows.set_index("sec_code") if not activity.rows.empty else pd.DataFrame()
    names = (
        context.universe.assign(sec_code=context.universe["sec_code"].astype(str).str.zfill(6))
        .set_index("sec_code")["sec_name"]
        .astype(str)
        .to_dict()
        if "sec_name" in context.universe.columns
        else {}
    )
    skipped = Counter(activity.skipped_reason_counts)
    output_columns = [
        "sec_code", "sec_name", "reference_price", "box_width", "close_box_width",
        "recent_atr_ratio", "prior_atr_ratio", "contraction_ratio", "recent_close_width",
        "prior_close_width", "breakout_level", "breakout_pct", "close_position",
        "activity_source", "activity_pct", "trend_profile",
    ]
    rows: list[dict[str, Any]] = []
    for code, frame in context.bars.groupby(context.bars["sec_code"].astype(str).str.zfill(6), sort=True):
        if activity_by_code.empty or code not in activity_by_code.index:
            continue
        indexed = frame.assign(trade_date=frame["trade_date"].astype(str)).set_index("trade_date").reindex(dates)
        close = pd.to_numeric(indexed.get("adj_close"), errors="coerce")
        high = pd.to_numeric(indexed.get("adj_high", indexed.get("high")), errors="coerce")
        low = pd.to_numeric(indexed.get("adj_low", indexed.get("low")), errors="coerce")
        derived_change = (close / close.shift(1) - 1.0) * 100.0
        if not all(series.notna().iloc[-required:].all() for series in (close, high, low, derived_change)):
            skipped["price_window_missing"] += 1
            continue
        base_high = float(high.iloc[-61:-1].max())
        base_low = float(low.iloc[-61:-1].min())
        close_box_high = float(close.iloc[-61:-1].max())
        close_box_low = float(close.iloc[-61:-1].min())
        box_width = base_high / base_low - 1.0 if base_low > 0 else math.nan
        close_box_width = close_box_high / close_box_low - 1.0 if close_box_low > 0 else math.nan
        previous_close = close.shift(1)
        true_range = pd.concat(
            [high - low, (high - previous_close).abs(), (low - previous_close).abs()], axis=1
        ).max(axis=1)
        atr_ratio = true_range / close
        recent_atr = float(atr_ratio.iloc[-11:-1].mean())
        prior_atr = float(atr_ratio.iloc[-51:-11].mean())
        recent_close_width = float(close.iloc[-11:-1].max() / close.iloc[-11:-1].min() - 1.0)
        prior_close_width = float(close.iloc[-51:-11].max() / close.iloc[-51:-11].min() - 1.0)
        contraction_ratio = recent_atr / prior_atr if prior_atr > 0 else math.nan
        current_close = float(close.iloc[-1])
        current_high = float(high.iloc[-1])
        current_low = float(low.iloc[-1])
        current_change = float(derived_change.iloc[-1])
        range_width = current_high - current_low
        if range_width <= 0:
            skipped["confirmation_path_unknown"] += 1
            continue
        close_position = (current_close - current_low) / range_width
        ma_middle = close.rolling(profile.middle_window, min_periods=profile.middle_window).mean()
        ma_long = close.rolling(profile.long_window, min_periods=profile.long_window).mean()
        middle_ok = current_close > float(ma_middle.iloc[-1]) and float(ma_middle.iloc[-1]) >= float(ma_middle.iloc[-2])
        long_ok = current_close > float(ma_long.iloc[-1]) and float(ma_long.iloc[-1]) >= float(ma_long.iloc[-2])
        qualifies = (
            math.isfinite(box_width)
            and box_width <= 0.35
            and recent_atr < prior_atr
            and recent_close_width < prior_close_width
            and current_close >= base_high * 1.005
            and current_change >= 3.0
            and close_position >= 0.75
            and middle_ok
            and long_ok
        )
        if not qualifies:
            skipped["base_breakout_gate_failed"] += 1
            continue
        rows.append(
            {
                "sec_code": code,
                "sec_name": names.get(code, code),
                "reference_price": current_close,
                "box_width": box_width,
                "close_box_width": close_box_width,
                "recent_atr_ratio": recent_atr,
                "prior_atr_ratio": prior_atr,
                "contraction_ratio": contraction_ratio,
                "recent_close_width": recent_close_width,
                "prior_close_width": prior_close_width,
                "breakout_level": base_high,
                "breakout_pct": current_close / base_high - 1.0,
                "close_position": close_position,
                "activity_source": str(activity_by_code.at[code, "activity_source"]),
                "activity_pct": float(activity_by_code.at[code, "activity_pct"]),
                "trend_profile": profile.profile_id,
            }
        )
    result = pd.DataFrame(rows, columns=output_columns)
    if not result.empty:
        result = result.sort_values(
            ["close_box_width", "contraction_ratio", "activity_pct", "sec_code"],
            ascending=[True, True, False, True],
        ).reset_index(drop=True)
    diagnostics["result_count"] = len(result)
    diagnostics["skipped_reason_counts"] = dict(sorted((key, value) for key, value in skipped.items() if value))
    return result, diagnostics


def select_base_breakout_from_context(context: SelectionContext) -> list[Candidate]:
    rows, _ = evaluate_base_breakout(context)
    return [
        Candidate(
            strategy_id=BaseBreakoutScanner.strategy_id,
            version=BaseBreakoutScanner.version,
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
class BaseBreakoutScanner(Scanner):
    strategy_id = "base_breakout"
    version = SCREENING_DEFINITION_VERSION
    kind = "stock"
    description = "V2 medium-base breakout with T-1-defined base and shared activity."
    default_params: dict[str, Any] = {}

    def run(self, conn: sqlite3.Connection, trade_date: str) -> list[Candidate]:
        context = build_selection_context(conn, trade_date, mode="close_final")
        self.last_universe_size = len(context.universe)
        return select_base_breakout_from_context(context)
