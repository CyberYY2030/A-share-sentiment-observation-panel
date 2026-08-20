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
    required = max(41, profile.long_window + 1)
    if len(dates) < required:
        diagnostics["skipped_reason_counts"] = {"insufficient_clean_history": len(context.universe)}
        return pd.DataFrame(), diagnostics
    activity = build_event_activity(
        context, baseline_days=20, required_window_days=21
    )
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
        "platform_days", "resistance_touches", "activity_source", "activity_pct", "amount", "score", "trend_profile",
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
        previous_close = close.shift(1)
        true_range = pd.concat(
            [high - low, (high - previous_close).abs(), (low - previous_close).abs()], axis=1
        ).max(axis=1)
        atr_ratio = true_range / close
        recent_atr = float(atr_ratio.iloc[-11:-1].mean())
        prior_atr = float(atr_ratio.iloc[-41:-1].mean())
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
        board = str(context.universe.set_index("sec_code").get("board", pd.Series(dtype=object)).to_dict().get(code) or "main")
        max_width = 0.20 if board in {"gem", "star"} else 0.15
        max_breakout = 0.12 if board in {"gem", "star"} else 0.08
        platform: tuple[int, float, float, float, float, int] | None = None
        for days in range(40, 14, -1):
            platform_high = float(high.iloc[-days - 1:-1].max())
            platform_low = float(low.iloc[-days - 1:-1].min())
            platform_close_high = float(close.iloc[-days - 1:-1].max())
            platform_close_low = float(close.iloc[-days - 1:-1].min())
            width = platform_high / platform_low - 1.0 if platform_low > 0 else math.nan
            touches = int(high.iloc[-days - 1:-1].ge(platform_high * 0.985).sum())
            if math.isfinite(width) and width <= max_width and touches >= 2:
                platform = (days, platform_high, platform_low, platform_close_high, platform_close_low, touches)
                break
        if platform is None:
            skipped["base_breakout_gate_failed"] += 1
            continue
        platform_days, base_high, base_low, close_box_high, close_box_low, resistance_touches = platform
        box_width = base_high / base_low - 1.0
        close_box_width = close_box_high / close_box_low - 1.0
        recent_close_width = float(close.iloc[-11:-1].max() / close.iloc[-11:-1].min() - 1.0)
        prior_close_width = float(close.iloc[-41:-11].max() / close.iloc[-41:-11].min() - 1.0)
        activity_pct = float(activity_by_code.at[code, "activity_pct"])
        amount = pd.to_numeric(indexed.get("amount"), errors="coerce").iloc[-1] if "amount" in indexed.columns else math.nan
        qualifies = (
            contraction_ratio <= 0.75
            and current_close >= base_high * 1.005
            and current_change >= 0.5
            and current_change <= max_breakout * 100
            and current_close / current_high >= 0.97
            and activity_pct >= 0.80
            and pd.notna(amount) and float(amount) >= 100_000_000
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
                "platform_days": platform_days,
                "resistance_touches": resistance_touches,
                "activity_source": str(activity_by_code.at[code, "activity_source"]),
                "activity_pct": activity_pct,
                "amount": float(amount),
                "score": activity_pct + (1.0 - contraction_ratio) + (1.0 - box_width),
                "trend_profile": profile.profile_id,
            }
        )
    result = pd.DataFrame(rows, columns=output_columns)
    if not result.empty:
        result = result.sort_values(
            ["score", "activity_pct", "sec_code"],
            ascending=[False, False, True],
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
