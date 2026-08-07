from __future__ import annotations

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
    columns = [
        "sec_code",
        "activity_source",
        "activity_value",
        "activity_ratio",
        "activity_pct",
        "activity_provisional",
    ]
    cache_key = f"event_activity:{int(baseline_days)}"
    cached = context.evaluation_cache.get(cache_key)
    if cached is not None:
        return EventActivityResult(cached.rows.copy(), dict(cached.skipped_reason_counts))
    dates = [str(value) for value in context.diagnostics.get("clean_dates", [])]
    if str(context.trade_date) not in dates:
        dates.append(str(context.trade_date))
    if context.bars.empty or not dates:
        return EventActivityResult(pd.DataFrame(columns=columns), {"activity_missing": len(context.universe)})

    mode = str(context.mode)
    normalized = context.bars.copy()
    normalized["sec_code"] = normalized["sec_code"].astype(str).str.zfill(6)
    normalized["trade_date"] = normalized["trade_date"].astype(str)
    codes = context.universe.get("sec_code", pd.Series(dtype=str)).astype(str).str.zfill(6).drop_duplicates().tolist()
    if not codes:
        return EventActivityResult(pd.DataFrame(columns=columns), {"activity_missing": 0})

    normalized = normalized[normalized["sec_code"].isin(codes) & normalized["trade_date"].isin(dates)]
    selected_source = None
    selected_values: pd.DataFrame | None = None
    for source in ("amount", "turnover_ratio"):
        if source not in normalized.columns:
            continue
        values = normalized.pivot(index="trade_date", columns="sec_code", values=source).reindex(index=dates, columns=codes)
        values = values.apply(pd.to_numeric, errors="coerce")
        history = values.iloc[-1 - baseline_days : -1]
        current = values.iloc[-1] if len(values) else pd.Series(index=codes, dtype="float64")
        if (
            len(history) == baseline_days
            and history.notna().all().all()
            and history.gt(0).all().all()
            and current.notna().all()
            and current.gt(0).all()
        ):
            selected_source = source
            selected_values = values
            break

    if selected_source is None:
        return EventActivityResult(pd.DataFrame(columns=columns), {"activity_missing": len(codes)})

    history = selected_values.iloc[-1 - baseline_days : -1]
    current = selected_values.iloc[-1]
    baseline = history.median(axis=0)
    result = pd.DataFrame(
        {
            "sec_code": codes,
            "activity_source": selected_source,
            "activity_value": current.to_numpy(),
            "activity_ratio": (current / baseline).to_numpy(),
            "activity_provisional": mode == "intraday_snapshot",
        }
    )
    result["activity_pct"] = cross_section_percentile(result["activity_ratio"])
    activity = EventActivityResult(result.reindex(columns=columns), {})
    context.evaluation_cache[cache_key] = activity
    return EventActivityResult(activity.rows.copy(), {})
