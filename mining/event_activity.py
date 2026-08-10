from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

import numpy as np
import pandas as pd

from .selection_context import SelectionContext
from .trend_factors import cross_section_percentile


@dataclass
class EventActivityResult:
    rows: pd.DataFrame
    skipped_reason_counts: dict[str, int]


def build_event_activity(
    context: SelectionContext,
    *,
    baseline_days: int = 20,
    required_window_days: int | None = None,
) -> EventActivityResult:
    """Build per-stock activity inputs, excluding T from ratio baselines."""
    columns = [
        "sec_code",
        "activity_source",
        "activity_value",
        "activity_ratio",
        "activity_pct",
        "activity_provisional",
    ]
    baseline_days = int(baseline_days)
    required_window_days = int(
        baseline_days + 1 if required_window_days is None else required_window_days
    )
    if baseline_days < 1 or required_window_days < baseline_days + 1:
        raise ValueError("required_window_days must include T and the complete baseline")
    cache_key = f"event_activity:{baseline_days}:{required_window_days}"
    cached = context.evaluation_cache.get(cache_key)
    if cached is not None:
        return EventActivityResult(cached.rows.copy(), dict(cached.skipped_reason_counts))
    trade_date = str(context.trade_date)
    dates = list(
        dict.fromkeys(
            str(value)
            for value in context.diagnostics.get("clean_dates", [])
            if str(value) != trade_date
        )
    )
    dates.append(trade_date)
    codes = context.universe.get("sec_code", pd.Series(dtype=str)).astype(str).str.zfill(6).drop_duplicates().tolist()
    if not codes:
        return EventActivityResult(pd.DataFrame(columns=columns), {})
    if context.bars.empty or not dates:
        empty = EventActivityResult(
            pd.DataFrame(columns=columns), {"activity_missing": len(codes)}
        )
        context.evaluation_cache[cache_key] = empty
        return EventActivityResult(empty.rows.copy(), dict(empty.skipped_reason_counts))

    mode = str(context.mode)
    normalized = context.bars.copy()
    normalized["sec_code"] = normalized["sec_code"].astype(str).str.zfill(6)
    normalized["trade_date"] = normalized["trade_date"].astype(str)
    normalized = normalized[normalized["sec_code"].isin(codes) & normalized["trade_date"].isin(dates)]
    source_values: dict[str, pd.DataFrame] = {}
    for source in ("amount", "turnover_ratio"):
        if source in normalized.columns:
            values = normalized.pivot_table(
                index="trade_date", columns="sec_code", values=source, aggfunc="last"
            ).reindex(index=dates, columns=codes)
            source_values[source] = values.apply(pd.to_numeric, errors="coerce")
        else:
            source_values[source] = pd.DataFrame(np.nan, index=dates, columns=codes)

    def complete_codes(source: str, window_days: int) -> pd.Series:
        if len(dates) < window_days:
            return pd.Series(False, index=codes, dtype=bool)
        window = source_values[source].iloc[-window_days:]
        finite = pd.DataFrame(
            np.isfinite(window.to_numpy(dtype=float)),
            index=window.index,
            columns=window.columns,
        )
        return (finite & window.gt(0)).all(axis=0).reindex(codes, fill_value=False)

    required_complete = {
        source: complete_codes(source, required_window_days)
        for source in ("amount", "turnover_ratio")
    }
    minimum_window_days = baseline_days + 1
    minimum_complete = (
        required_complete
        if required_window_days == minimum_window_days
        else {
            source: complete_codes(source, minimum_window_days)
            for source in ("amount", "turnover_ratio")
        }
    )

    selected_sources: dict[str, str] = {}
    skipped = Counter()
    for code in codes:
        source = next(
            (
                candidate
                for candidate in ("amount", "turnover_ratio")
                if bool(required_complete[candidate].get(code, False))
            ),
            None,
        )
        if source is not None:
            selected_sources[code] = source
        elif any(bool(minimum_complete[candidate].get(code, False)) for candidate in minimum_complete):
            skipped["activity_window_missing"] += 1
        else:
            skipped["activity_missing"] += 1

    rows: list[dict[str, object]] = []
    for code, source in selected_sources.items():
        values = source_values[source][code]
        history = values.iloc[-1 - baseline_days : -1]
        current = float(values.iloc[-1])
        baseline = float(history.median())
        rows.append(
            {
                "sec_code": code,
                "activity_source": source,
                "activity_value": current,
                "activity_ratio": current / baseline,
                "activity_provisional": mode == "intraday_snapshot",
            }
        )
    result = pd.DataFrame(rows, columns=[column for column in columns if column != "activity_pct"])
    result["activity_pct"] = cross_section_percentile(result["activity_ratio"])
    activity = EventActivityResult(
        result.reindex(columns=columns),
        dict(sorted((reason, count) for reason, count in skipped.items() if count)),
    )
    context.evaluation_cache[cache_key] = activity
    return EventActivityResult(activity.rows.copy(), dict(activity.skipped_reason_counts))
