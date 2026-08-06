from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
import pandas as pd


PRICE_COLUMNS = ("open", "high", "low", "close")
ADJUSTED_PRICE_COLUMNS = tuple(f"adj_{column}" for column in (*PRICE_COLUMNS, "pre_close"))


@dataclass
class AdjustedPriceResult:
    bars: pd.DataFrame
    invalid_code_reasons: dict[str, list[str]]
    skipped_reason_counts: dict[str, int]
    fallback_counts: dict[str, int]


def build_forward_adjusted_bars(
    bars: pd.DataFrame,
    *,
    price_tick: float = 0.01,
    min_step_ratio: float = 0.1,
    max_step_ratio: float = 10.0,
) -> AdjustedPriceResult:
    """Return research-only forward-adjusted OHLC without changing raw activity facts.

    The final valid bar for each stock is anchored at factor ``1``.  An invalid
    corporate-action edge invalidates that stock's adjusted series.  A genuinely
    missing ``pre_close`` uses the previous valid close as a neutral edge and is
    retained in diagnostics rather than being silently fabricated.
    """
    required = {"sec_code", "trade_date", "pre_close", *PRICE_COLUMNS}
    missing = required.difference(bars.columns)
    if missing:
        raise ValueError(f"adjusted prices require columns: {sorted(missing)}")
    if not (price_tick > 0 and min_step_ratio > 0 and max_step_ratio >= min_step_ratio):
        raise ValueError("invalid adjusted-price thresholds")
    if bars.empty:
        output = bars.copy()
        for column in ("adjustment_factor", "adjustment_valid", *ADJUSTED_PRICE_COLUMNS):
            output[column] = pd.Series(dtype="float64")
        return AdjustedPriceResult(output, {}, {}, {})

    normalized = bars.copy()
    normalized["sec_code"] = normalized["sec_code"].astype(str).str.zfill(6)
    ordered = normalized.sort_values(["sec_code", "trade_date"], kind="stable").reset_index(drop=True)
    prices = {column: pd.to_numeric(ordered[column], errors="coerce") for column in PRICE_COLUMNS}
    positive_prices = {column: values.gt(0) & values.map(math.isfinite) for column, values in prices.items()}
    ohlc_valid = (
        positive_prices["open"]
        & positive_prices["high"]
        & positive_prices["low"]
        & positive_prices["close"]
        & prices["low"].le(pd.concat([prices["open"], prices["close"]], axis=1).min(axis=1))
        & pd.concat([prices["open"], prices["close"]], axis=1).max(axis=1).le(prices["high"])
    )
    close_valid = positive_prices["close"]
    codes = ordered["sec_code"]
    previous_valid_close = prices["close"].where(close_valid).groupby(codes, sort=False).ffill().groupby(codes, sort=False).shift()
    has_previous = previous_valid_close.notna()
    pre_close = pd.to_numeric(ordered["pre_close"], errors="coerce")
    pre_close_missing = pre_close.isna()
    pre_close_valid = pre_close.gt(0) & pre_close.map(math.isfinite)
    invalid_pre_close = has_previous & ~pre_close_missing & ~pre_close_valid
    effective_pre_close = pre_close.where(~(has_previous & pre_close_missing), previous_valid_close)
    raw_ratio = effective_pre_close / previous_valid_close
    tick_sized = (effective_pre_close - previous_valid_close).abs().le(
        pd.concat(
            [pd.Series(float(price_tick), index=ordered.index), previous_valid_close * 1e-9],
            axis=1,
        ).max(axis=1)
    )
    step_ratios = raw_ratio.where(~tick_sized, 1.0).where(has_previous, 1.0)
    ratio_valid = step_ratios.map(math.isfinite) & step_ratios.between(float(min_step_ratio), float(max_step_ratio))
    invalid_ratio = has_previous & ~pre_close_missing & pre_close_valid & ~ratio_valid

    reason_masks = {
        "invalid_ohlc": ~ohlc_valid,
        "invalid_close": ~close_valid,
        "invalid_pre_close": invalid_pre_close,
        "adjustment_ratio_out_of_range": invalid_ratio,
    }
    code_reasons: dict[str, list[str]] = {}
    reason_counts: Counter[str] = Counter()
    for reason, mask in reason_masks.items():
        invalid_codes = sorted({str(code) for code in codes[mask]})
        if invalid_codes:
            reason_counts[reason] = len(invalid_codes)
            for code in invalid_codes:
                code_reasons.setdefault(code, []).append(reason)

    reverse = pd.DataFrame({"sec_code": codes, "step_ratio": step_ratios}).iloc[::-1]
    reverse_product = reverse.groupby("sec_code", sort=False)["step_ratio"].cumprod()
    future_product = reverse_product.reindex(ordered.index)
    ordered["adjustment_factor"] = future_product / step_ratios
    for column in PRICE_COLUMNS:
        ordered[f"adj_{column}"] = prices[column] * ordered["adjustment_factor"]
    ordered["adjustment_pre_close_source"] = "reported"
    ordered.loc[has_previous & pre_close_missing, "adjustment_pre_close_source"] = "previous_valid_close"
    ordered["adj_pre_close"] = effective_pre_close * ordered["adjustment_factor"]
    ordered["adjustment_valid"] = ~codes.isin(code_reasons)
    if code_reasons:
        invalid_rows = ~ordered["adjustment_valid"]
        ordered.loc[invalid_rows, "adjustment_factor"] = math.nan
        for column in ADJUSTED_PRICE_COLUMNS:
            ordered.loc[invalid_rows, column] = math.nan

    fallback_counts: Counter[str] = Counter()
    missing_pre_close_count = int((has_previous & pre_close_missing).sum())
    if missing_pre_close_count:
        fallback_counts["missing_pre_close_used_previous_valid_close"] = missing_pre_close_count
    result = ordered.sort_values(["trade_date", "sec_code"], kind="stable")
    return AdjustedPriceResult(
        bars=result.reset_index(drop=True),
        invalid_code_reasons=code_reasons,
        skipped_reason_counts=dict(sorted(reason_counts.items())),
        fallback_counts=dict(sorted(fallback_counts.items())),
    )
