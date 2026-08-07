from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd


PRICE_COLUMNS = ("open", "high", "low", "close")
ADJUSTED_PRICE_COLUMNS = tuple(f"adj_{column}" for column in (*PRICE_COLUMNS, "pre_close"))
_PRICE_USABLE_STATUSES = {"valid_trade", "confirmed_halt", "provider_halt_placeholder"}


@dataclass
class AdjustedPriceResult:
    bars: pd.DataFrame
    invalid_code_reasons: dict[str, list[str]]
    skipped_reason_counts: dict[str, int]
    fallback_counts: dict[str, int]


def _finite_positive(value: Any) -> bool:
    try:
        return math.isfinite(float(value)) and float(value) > 0.0
    except (TypeError, ValueError):
        return False


def _provider_zero_no_activity_placeholder(numeric: dict[str, pd.Series]) -> pd.Series:
    return (
        numeric["open"].eq(0)
        & numeric["high"].eq(0)
        & numeric["low"].eq(0)
        & numeric["close"].eq(0)
        & numeric["pre_close"].gt(0)
        & (numeric["volume"].eq(0) | numeric["volume"].isna())
        & (numeric["amount"].eq(0) | numeric["amount"].isna())
    )


def _derived_row_statuses(frame: pd.DataFrame) -> pd.Series:
    numeric = {column: pd.to_numeric(frame[column], errors="coerce") for column in (*PRICE_COLUMNS, "pre_close", "volume", "amount")}
    finite_positive = {column: numeric[column].gt(0) & np.isfinite(numeric[column]) for column in numeric}
    price_valid = finite_positive["open"] & finite_positive["high"] & finite_positive["low"] & finite_positive["close"]
    ohlc_valid = (
        price_valid
        & numeric["low"].le(pd.concat([numeric["open"], numeric["close"]], axis=1).min(axis=1))
        & pd.concat([numeric["open"], numeric["close"]], axis=1).max(axis=1).le(numeric["high"])
    )
    activity_present = numeric["volume"].notna() & numeric["amount"].notna()
    valid_trade = ohlc_valid & finite_positive["volume"] & finite_positive["amount"]
    flat_prices = numeric["open"].eq(numeric["high"]) & numeric["high"].eq(numeric["low"]) & numeric["low"].eq(numeric["close"])
    provider_halt = (
        ohlc_valid
        & numeric["volume"].eq(0)
        & numeric["amount"].eq(0)
        & finite_positive["pre_close"]
        & flat_prices
        & numeric["pre_close"].eq(numeric["close"])
    )
    provider_zero = _provider_zero_no_activity_placeholder(numeric)
    statuses = pd.Series("invalid_price", index=frame.index, dtype="object")
    statuses.loc[ohlc_valid & activity_present] = "invalid_activity"
    statuses.loc[ohlc_valid & ~activity_present] = "missing"
    statuses.loc[provider_halt] = "provider_halt_placeholder"
    statuses.loc[provider_zero] = "provider_halt_placeholder"
    statuses.loc[valid_trade] = "valid_trade"
    return statuses


def _collapse_duplicate_rows(frame: pd.DataFrame) -> tuple[pd.DataFrame, Counter[str]]:
    reasons: Counter[str] = Counter()
    keys = ["sec_code", "trade_date"]
    duplicate_mask = frame.duplicated(keys, keep=False)
    if not bool(duplicate_mask.any()):
        return frame.reset_index(drop=True), reasons

    output = frame.drop_duplicates(keys, keep="first").copy()
    value_columns = ["open", "high", "low", "close", "pre_close", "volume", "amount"]
    duplicate_rows = frame.loc[duplicate_mask]
    for key, group in duplicate_rows.groupby(keys, sort=False, dropna=False):
        signatures = {
            tuple("nan" if pd.isna(value) else f"{float(value):.12g}" if isinstance(value, (int, float)) else str(value) for value in row[value_columns])
            for _, row in group.iterrows()
        }
        selector = output["sec_code"].eq(key[0]) & output["trade_date"].eq(key[1])
        if len(signatures) > 1:
            output.loc[selector, "row_status"] = "invalid_price"
            output.loc[selector, "row_reason"] = "conflicting_duplicate"
            reasons["conflicting_duplicate"] += 1
        else:
            reasons["identical_duplicate_collapsed"] += len(group) - 1
    return output.reset_index(drop=True), reasons


def _flush_segment(segment: list[dict[str, Any]], output: pd.DataFrame) -> None:
    if not segment:
        return
    steps = [float(item["step_ratio"]) for item in segment]
    future_product = 1.0
    factors = [1.0] * len(segment)
    for index in range(len(segment) - 1, -1, -1):
        future_product *= steps[index]
        factors[index] = future_product / steps[index]
    indexes = [int(item["index"]) for item in segment]
    segment_start = str(output.loc[indexes[0], "trade_date"])
    output.loc[indexes, "adjustment_factor"] = factors
    output.loc[indexes, "adjustment_valid"] = True
    output.loc[indexes, "valid_adjusted_bar"] = True
    output.loc[indexes, "latest_valid_segment_start"] = segment_start
    output.loc[indexes, "adjustment_pre_close_source"] = [item["pre_close_source"] for item in segment]
    factor_series = pd.Series(factors, index=indexes, dtype="float64")
    for column in PRICE_COLUMNS:
        prices = pd.Series(
            [
                item.get("calculation_prices", {}).get(column, output.at[int(item["index"]), column])
                for item in segment
            ],
            index=indexes,
            dtype="float64",
        )
        output.loc[indexes, f"adj_{column}"] = prices * factor_series
    output.loc[indexes, "adj_pre_close"] = pd.Series(
        [float(item["effective_pre_close"]) for item in segment], index=indexes, dtype="float64"
    ) * factor_series


def _apply_simple_valid_trade_adjustments(
    output: pd.DataFrame,
    *,
    price_tick: float,
    min_step_ratio: float,
    max_step_ratio: float,
) -> tuple[set[str], int]:
    """Vectorize codes whose rows never need the quarantine/fallback path."""
    numeric = {column: pd.to_numeric(output[column], errors="coerce") for column in (*PRICE_COLUMNS, "pre_close")}
    codes = output["sec_code"]
    starts = output.groupby("sec_code", sort=False).cumcount().eq(0)
    previous_close = numeric["close"].groupby(codes, sort=False).shift()
    reported_pre_close = numeric["pre_close"].gt(0) & np.isfinite(numeric["pre_close"])
    missing_pre_close = numeric["pre_close"].isna()
    effective_pre_close = numeric["pre_close"].where(reported_pre_close, previous_close).where(~starts, numeric["close"])
    raw_ratio = effective_pre_close / previous_close
    tick_sized = (effective_pre_close - previous_close).abs().le(
        np.maximum(float(price_tick), previous_close * 1e-9)
    )
    steps = raw_ratio.where(~tick_sized, 1.0).where(~starts, 1.0)
    valid_step = (
        starts
        | (
            (reported_pre_close | missing_pre_close)
            & np.isfinite(previous_close)
            & np.isfinite(steps)
            & steps.between(float(min_step_ratio), float(max_step_ratio), inclusive="both")
        )
    )
    simple_rows = output["row_status"].eq("valid_trade") & valid_step
    simple_codes = set(codes.loc[simple_rows.groupby(codes, sort=False).transform("all")].astype(str))
    if not simple_codes:
        return set(), 0

    fast = output.loc[output["sec_code"].isin(simple_codes)].copy()
    fast_starts = fast.groupby("sec_code", sort=False).cumcount().eq(0)
    fast_previous_close = numeric["close"].loc[fast.index].groupby(fast["sec_code"], sort=False).shift()
    fast_reported_pre_close = reported_pre_close.loc[fast.index]
    fast_effective_pre_close = numeric["pre_close"].loc[fast.index].where(
        fast_reported_pre_close, fast_previous_close
    ).where(~fast_starts, numeric["close"].loc[fast.index])
    fast_raw_ratio = fast_effective_pre_close / fast_previous_close
    fast_tick_sized = (fast_effective_pre_close - fast_previous_close).abs().le(
        np.maximum(float(price_tick), fast_previous_close * 1e-9)
    )
    fast_steps = fast_raw_ratio.where(~fast_tick_sized, 1.0).where(~fast_starts, 1.0)
    future_products = fast_steps.iloc[::-1].groupby(fast["sec_code"].iloc[::-1], sort=False).cumprod().iloc[::-1]
    factors = future_products / fast_steps
    segment_starts = fast.groupby("sec_code", sort=False)["trade_date"].transform("first").astype(str)

    output.loc[fast.index, "adjustment_factor"] = factors
    output.loc[fast.index, "adjustment_valid"] = True
    output.loc[fast.index, "valid_adjusted_bar"] = True
    output.loc[fast.index, "latest_valid_segment_start"] = segment_starts
    output.loc[fast.index, "adjustment_pre_close_source"] = np.where(
        fast_starts,
        "segment_anchor",
        np.where(fast_reported_pre_close, "reported", "previous_valid_close"),
    )
    for column in PRICE_COLUMNS:
        output.loc[fast.index, f"adj_{column}"] = numeric[column].loc[fast.index] * factors
    output.loc[fast.index, "adj_pre_close"] = fast_effective_pre_close * factors
    fallback_count = int((~fast_starts & ~fast_reported_pre_close).sum())
    return simple_codes, fallback_count


def build_forward_adjusted_bars(
    bars: pd.DataFrame,
    *,
    price_tick: float = 0.01,
    min_step_ratio: float = 0.1,
    max_step_ratio: float = 10.0,
    allow_missing_activity: bool = False,
) -> AdjustedPriceResult:
    """Build forward-adjusted bars without allowing one bad row to erase a stock.

    Every invalid row or invalid corporate-action edge ends only its current
    contiguous segment. Consumers receive all valid segments but must use the
    latest one, so MA/RPS cannot cross the missing edge.
    """
    required = {"sec_code", "trade_date", "pre_close", *PRICE_COLUMNS}
    missing = required.difference(bars.columns)
    if missing:
        raise ValueError(f"adjusted prices require columns: {sorted(missing)}")
    if not (price_tick > 0 and min_step_ratio > 0 and max_step_ratio >= min_step_ratio):
        raise ValueError("invalid adjusted-price thresholds")
    if bars.empty:
        output = bars.copy()
        for column in (
            "row_status", "row_reason", "adjustment_factor", "adjustment_valid", "valid_adjusted_bar",
            "latest_valid_segment_start", "adjustment_pre_close_source", *ADJUSTED_PRICE_COLUMNS,
        ):
            output[column] = pd.Series(dtype="object")
        return AdjustedPriceResult(output, {}, {}, {})

    normalized = bars.copy()
    normalized["sec_code"] = normalized["sec_code"].astype(str).str.zfill(6)
    normalized = normalized.sort_values(["sec_code", "trade_date"], kind="stable").reset_index(drop=True)
    if "row_status" not in normalized.columns:
        normalized["row_status"] = _derived_row_statuses(normalized)
    normalized["row_status"] = normalized["row_status"].fillna("invalid_price").astype(str)
    if "row_reason" not in normalized.columns:
        normalized["row_reason"] = pd.NA
    output, duplicate_counts = _collapse_duplicate_rows(normalized)
    output_zero_placeholder = _provider_zero_no_activity_placeholder(
        {column: pd.to_numeric(output[column], errors="coerce") for column in (*PRICE_COLUMNS, "pre_close", "volume", "amount")}
    )
    output.loc[
        output_zero_placeholder & output["row_status"].eq("provider_halt_placeholder"),
        "row_reason",
    ] = "provider_zero_no_activity_placeholder"
    output["adjustment_factor"] = math.nan
    output["adjustment_valid"] = False
    output["valid_adjusted_bar"] = False
    output["latest_valid_segment_start"] = pd.NA
    output["adjustment_pre_close_source"] = pd.NA
    for column in ADJUSTED_PRICE_COLUMNS:
        output[column] = math.nan

    reasons: Counter[str] = Counter(duplicate_counts)
    fallback_counts: Counter[str] = Counter()
    invalid_code_reasons: dict[str, list[str]] = {}
    simple_codes, simple_fallback_count = _apply_simple_valid_trade_adjustments(
        output,
        price_tick=price_tick,
        min_step_ratio=min_step_ratio,
        max_step_ratio=max_step_ratio,
    )
    if simple_fallback_count:
        fallback_counts["missing_pre_close_used_previous_valid_close"] += simple_fallback_count
    for code, group in output.groupby("sec_code", sort=False):
        if str(code) in simple_codes:
            continue
        segment: list[dict[str, Any]] = []
        indexes = group.index.to_list()
        statuses = group["row_status"].astype(str).tolist()
        closes = pd.to_numeric(group["close"], errors="coerce").tolist()
        pre_closes = pd.to_numeric(group["pre_close"], errors="coerce").tolist()
        reasons_by_index = group["row_reason"].tolist()
        price_rows = group.loc[:, PRICE_COLUMNS].to_dict("records")
        for position, index in enumerate(indexes):
            status = statuses[position]
            price_only_snapshot = (
                allow_missing_activity
                and status == "missing"
                and all(_finite_positive(price_rows[position][column]) for column in PRICE_COLUMNS)
            )
            if status not in _PRICE_USABLE_STATUSES and not price_only_snapshot:
                raw_reason = reasons_by_index[position]
                reason = status if pd.isna(raw_reason) else str(raw_reason)
                reasons[reason] += 1
                invalid_code_reasons.setdefault(str(code), []).append(reason)
                _flush_segment(segment, output)
                segment = []
                continue

            if not segment:
                if status == "provider_halt_placeholder":
                    output.loc[index, "row_reason"] = "provider_halt_without_prior_valid_close"
                    reasons["provider_halt_without_prior_valid_close"] += 1
                    invalid_code_reasons.setdefault(str(code), []).append("provider_halt_without_prior_valid_close")
                    continue
                close = float(closes[position])
                segment.append(
                    {
                        "index": int(index),
                        "close": close,
                        "step_ratio": 1.0,
                        "effective_pre_close": close,
                        "pre_close_source": "segment_anchor",
                    }
                )
                continue

            close = float(closes[position])
            previous_close = float(segment[-1]["close"])
            pre_close = pre_closes[position]
            if status in {"confirmed_halt", "provider_halt_placeholder"}:
                effective_pre_close, source = previous_close, "halt_previous_valid_close"
                if status == "provider_halt_placeholder":
                    close = previous_close
            elif pd.isna(pre_close):
                effective_pre_close, source = previous_close, "previous_valid_close"
                fallback_counts["missing_pre_close_used_previous_valid_close"] += 1
            elif not _finite_positive(pre_close):
                output.loc[index, "row_reason"] = "invalid_pre_close"
                reasons["invalid_pre_close"] += 1
                invalid_code_reasons.setdefault(str(code), []).append("invalid_pre_close")
                _flush_segment(segment, output)
                segment = []
                continue
            else:
                effective_pre_close, source = float(pre_close), "reported"

            raw_ratio = effective_pre_close / previous_close
            tick_sized = abs(effective_pre_close - previous_close) <= max(float(price_tick), previous_close * 1e-9)
            step_ratio = 1.0 if tick_sized else raw_ratio
            if not math.isfinite(step_ratio) or not (float(min_step_ratio) <= step_ratio <= float(max_step_ratio)):
                output.loc[index, "row_reason"] = "adjustment_ratio_out_of_range"
                reasons["adjustment_ratio_out_of_range"] += 1
                invalid_code_reasons.setdefault(str(code), []).append("adjustment_ratio_out_of_range")
                _flush_segment(segment, output)
                segment = []
                continue
            item = {
                "index": int(index),
                "close": close,
                "step_ratio": step_ratio,
                "effective_pre_close": effective_pre_close,
                "pre_close_source": source,
            }
            if status == "provider_halt_placeholder":
                item["calculation_prices"] = {column: previous_close for column in PRICE_COLUMNS}
            segment.append(item)
        _flush_segment(segment, output)

    for code, code_reasons in invalid_code_reasons.items():
        invalid_code_reasons[code] = sorted(set(code_reasons))
    result = output.sort_values(["trade_date", "sec_code"], kind="stable").reset_index(drop=True)
    return AdjustedPriceResult(
        bars=result,
        invalid_code_reasons=dict(sorted(invalid_code_reasons.items())),
        skipped_reason_counts=dict(sorted(reasons.items())),
        fallback_counts=dict(sorted(fallback_counts.items())),
    )
