from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

import pandas as pd

from .selection_context import SelectionContext
from .trend_factors import cross_section_percentile


@dataclass
class EventActivityResult:
    rows: pd.DataFrame
    skipped_reason_counts: dict[str, int]


def build_event_activity(context: SelectionContext, *, baseline_days: int = 20) -> EventActivityResult:
    """Build the one activity input shared by B/D, excluding T from close baselines."""
    columns = ["sec_code", "activity_source", "activity_value", "activity_ratio", "activity_pct"]
    dates = [str(value) for value in context.diagnostics.get("clean_dates", [])]
    if str(context.trade_date) not in dates:
        dates.append(str(context.trade_date))
    if context.bars.empty or not dates:
        return EventActivityResult(pd.DataFrame(columns=columns), {"activity_missing": len(context.universe)})

    rows: list[dict[str, object]] = []
    skipped = Counter()
    mode = str(context.mode)
    normalized = context.bars.copy()
    normalized["sec_code"] = normalized["sec_code"].astype(str).str.zfill(6)
    normalized["trade_date"] = normalized["trade_date"].astype(str)
    for code, frame in normalized.groupby("sec_code", sort=True):
        indexed = frame.set_index("trade_date").reindex(dates)
        selected: tuple[str, pd.Series] | None = None
        for source in ("turnover_ratio", "amount"):
            if source not in indexed.columns:
                continue
            values = pd.to_numeric(indexed[source], errors="coerce")
            current = values.iloc[-1]
            if mode == "intraday_snapshot":
                if pd.notna(current) and float(current) > 0:
                    selected = (source, values)
                    break
                continue
            history = values.iloc[-1 - baseline_days : -1]
            if len(history) == baseline_days and history.notna().all() and float(history.mean()) > 0 and pd.notna(current):
                selected = (source, values)
                break
        if selected is None:
            skipped["activity_missing"] += 1
            continue
        source, values = selected
        current = float(values.iloc[-1])
        if mode == "intraday_snapshot":
            ratio = None
        else:
            ratio = current / float(values.iloc[-1 - baseline_days : -1].mean())
        rows.append(
            {
                "sec_code": code,
                "activity_source": source,
                "activity_value": current,
                "activity_ratio": ratio,
            }
        )
    result = pd.DataFrame(rows, columns=columns[:-1])
    if result.empty:
        return EventActivityResult(pd.DataFrame(columns=columns), dict(skipped))
    if mode == "intraday_snapshot":
        result["activity_pct"] = pd.NA
        for source, index in result.groupby("activity_source").groups.items():
            result.loc[index, "activity_pct"] = cross_section_percentile(result.loc[index, "activity_value"])
    else:
        result["activity_pct"] = cross_section_percentile(result["activity_ratio"])
    return EventActivityResult(result, dict(sorted(skipped.items())))
