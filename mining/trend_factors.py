from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class TrendProfile:
    profile_id: str
    min_clean_sessions: int
    short_window: int
    middle_window: int
    long_window: int
    direction_shift: int
    intermediate_window: int | None = None

    @property
    def required_stock_bars(self) -> int:
        return self.long_window + self.direction_shift


TREND_PROFILES = (
    TrendProfile("P200", 220, 20, 60, 200, 20, intermediate_window=120),
    TrendProfile("P120", 140, 20, 60, 120, 20),
    TrendProfile("P90", 110, 20, 60, 90, 20),
    TrendProfile("P60", 75, 10, 30, 60, 15),
)


def resolve_trend_profile(clean_session_count: int) -> TrendProfile | None:
    """Select one market-wide profile; callers may not downgrade it per stock."""
    for profile in TREND_PROFILES:
        if int(clean_session_count) >= profile.min_clean_sessions:
            return profile
    return None


def evaluate_trend_structure(bars: pd.DataFrame, profile: TrendProfile | None) -> pd.DataFrame:
    """Evaluate the profile's moving-average gate on the latest clean session."""
    columns = [
        "sec_code",
        "history_sufficient",
        "trend_structure_pass",
        "adj_close",
        "ma_short",
        "ma_middle",
        "ma_intermediate",
        "ma_long",
        "ma_long_prior",
        "long_window_high",
        "near_high_ratio",
    ]
    history = evaluate_trend_structure_history(bars, profile)
    if history.empty:
        return pd.DataFrame(columns=columns)
    latest_date = history["trade_date"].astype(str).max()
    return history.loc[history["trade_date"].astype(str).eq(latest_date), columns].reset_index(drop=True)


def evaluate_trend_structure_history(bars: pd.DataFrame, profile: TrendProfile | None) -> pd.DataFrame:
    """Evaluate the shared trend gate at every clean session without compressing gaps."""
    columns = [
        "sec_code",
        "trade_date",
        "history_sufficient",
        "trend_structure_pass",
        "adj_close",
        "ma_short",
        "ma_middle",
        "ma_intermediate",
        "ma_long",
        "ma_long_prior",
        "long_window_high",
        "near_high_ratio",
    ]
    if profile is None or bars.empty or not {"sec_code", "trade_date", "adj_close"}.issubset(bars.columns):
        return pd.DataFrame(columns=columns)

    rows: list[dict[str, object]] = []
    normalized = bars[["sec_code", "trade_date", "adj_close"]].copy()
    normalized["sec_code"] = normalized["sec_code"].astype(str).str.zfill(6)
    normalized["trade_date"] = normalized["trade_date"].astype(str)
    normalized["adj_close"] = pd.to_numeric(normalized["adj_close"], errors="coerce")
    expected_dates = sorted(normalized["trade_date"].unique())
    for sec_code, frame in normalized.groupby("sec_code", sort=True):
        close = frame.set_index("trade_date")["adj_close"].reindex(expected_dates)
        ma_short = close.rolling(profile.short_window, min_periods=profile.short_window).mean()
        ma_middle = close.rolling(profile.middle_window, min_periods=profile.middle_window).mean()
        ma_intermediate = (
            close.rolling(profile.intermediate_window, min_periods=profile.intermediate_window).mean()
            if profile.intermediate_window is not None
            else None
        )
        ma_long = close.rolling(profile.long_window, min_periods=profile.long_window).mean()
        for position, trade_date in enumerate(expected_dates):
            tail_start = position - profile.required_stock_bars + 1
            history_sufficient = tail_start >= 0 and close.iloc[tail_start : position + 1].notna().all()
            latest_close = close.iloc[position]
            if not history_sufficient:
                rows.append(
                    {
                        "sec_code": sec_code,
                        "trade_date": trade_date,
                        "history_sufficient": False,
                        "trend_structure_pass": False,
                        "adj_close": latest_close if pd.notna(latest_close) else pd.NA,
                        "ma_short": pd.NA,
                        "ma_middle": pd.NA,
                        "ma_intermediate": pd.NA,
                        "ma_long": pd.NA,
                        "ma_long_prior": pd.NA,
                        "long_window_high": pd.NA,
                        "near_high_ratio": pd.NA,
                    }
                )
                continue
            latest_short = float(ma_short.iloc[position])
            latest_middle = float(ma_middle.iloc[position])
            latest_intermediate = float(ma_intermediate.iloc[position]) if ma_intermediate is not None else pd.NA
            latest_long = float(ma_long.iloc[position])
            previous_long = float(ma_long.iloc[position - profile.direction_shift])
            long_high = float(close.iloc[position - profile.long_window + 1 : position + 1].max())
            aligned = float(latest_close) > latest_short > latest_middle
            if ma_intermediate is not None:
                aligned = aligned and latest_middle > float(latest_intermediate) > latest_long
            else:
                aligned = aligned and latest_middle > latest_long
            rows.append(
                {
                    "sec_code": sec_code,
                    "trade_date": trade_date,
                    "history_sufficient": True,
                    "trend_structure_pass": aligned and latest_long > previous_long,
                    "adj_close": float(latest_close),
                    "ma_short": latest_short,
                    "ma_middle": latest_middle,
                    "ma_intermediate": latest_intermediate,
                    "ma_long": latest_long,
                    "ma_long_prior": previous_long,
                    "long_window_high": long_high,
                    "near_high_ratio": float(latest_close) / long_high if long_high > 0 else pd.NA,
                }
            )
    return pd.DataFrame(rows, columns=columns)


def cross_section_percentile(values: pd.Series) -> pd.Series:
    """Use one same-unit cross-sectional percentile, retaining missing inputs as missing."""
    numeric = pd.to_numeric(values, errors="coerce")
    return numeric.rank(pct=True, method="average")
