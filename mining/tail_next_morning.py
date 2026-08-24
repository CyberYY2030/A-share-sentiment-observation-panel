"""Fail-closed primitives for the Tail Next-Morning V1 research card.

This module deliberately stops at source validation and a fixed small preflight.
It does not select features, run a historical batch, write a database, or call a
provider.  Feature snapshots only receive information available at D 14:50;
outcomes are calculated by a separate function.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import json
import math
import os
import subprocess
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping

import pandas as pd
import numpy as np


MINUTE_COLUMNS = ("open", "high", "low", "close", "amount", "volume")
A_SHARE_PREFIXES = ("000", "001", "002", "003", "300", "301", "600", "601", "603", "605", "688", "689")
MIN_HISTORY_SESSIONS = 20
MIN_D1_AMOUNT = 500_000_000.0
CAPACITY_TOTAL_YUAN = 5_000_000.0
CAPACITY_PER_STOCK_YUAN = 500_000.0
CAPACITY_PARTICIPATION_LIMIT = 0.005
DAILY_MINUTE_AMOUNT_RATIO_TOLERANCE = 0.01
FEATURE_NAMES = (
    "tail_return_rel",
    "tail_end_location",
    "tail_amount_accel",
    "pre_tail_return_rel",
    "activity_ratio",
    "recent_3date_return_rel",
)
FEATURE_GROUPS = {
    "tail_price": ("tail_return_rel", "tail_end_location"),
    "tail_amount": ("tail_amount_accel",),
    "background": ("pre_tail_return_rel", "activity_ratio", "recent_3date_return_rel"),
}
FEATURE_NUMBER = {name: index for index, name in enumerate(FEATURE_NAMES, start=1)}
DEVELOPMENT_YEARS = ("2023", "2024")
RANDOM_BASELINE_SEED = "20260824"
SLOT_COUNT = 10


def _session_labels() -> tuple[str, ...]:
    morning = [f"{minute // 60:02d}:{minute % 60:02d}" for minute in range(9 * 60 + 30, 11 * 60 + 30)]
    afternoon = [f"{minute // 60:02d}:{minute % 60:02d}" for minute in range(13 * 60, 15 * 60)]
    return tuple(morning + afternoon)


SESSION_LABELS = _session_labels()
TIME_TO_INDEX = {label: index for index, label in enumerate(SESSION_LABELS)}


class TailDataError(ValueError):
    """A source or data-quality condition that must isolate the affected sample."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def canonical_code(value: object) -> str:
    digits = "".join(character for character in str(value) if character.isdigit())
    return digits[-6:].zfill(6) if digits else ""


def is_a_share_code(code: object) -> bool:
    normalized = canonical_code(code)
    return len(normalized) == 6 and normalized.startswith(A_SHARE_PREFIXES)


def _window_index(value: str) -> int:
    normalized = str(value).strip()[:5]
    if normalized == "15:00":
        return len(SESSION_LABELS)
    if normalized == "11:30":
        return 120
    try:
        return TIME_TO_INDEX[normalized]
    except KeyError as exc:
        raise TailDataError(f"unsupported_window_boundary:{value}") from exc


def window_bars(bars: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    """Return the left-closed/right-open exchange-minute window."""
    start_index = _window_index(start)
    end_index = _window_index(end)
    if start_index >= end_index:
        raise TailDataError(f"invalid_window:{start}:{end}")
    return bars.iloc[start_index:end_index].copy()


def parse_minute_frame(raw: pd.DataFrame) -> pd.DataFrame:
    """Parse fixed session structure without making an outcome window a signal gate."""
    if raw is None:
        raise TailDataError("minute_frame_missing")
    columns_by_lower = {str(column).strip().lower(): column for column in raw.columns}
    missing = [name for name in MINUTE_COLUMNS if name not in columns_by_lower]
    if missing:
        raise TailDataError(f"minute_columns_missing:{','.join(missing)}")
    if len(raw) != len(SESSION_LABELS):
        raise TailDataError(f"session_bar_count:{len(raw)}")

    time_column = next(
        (columns_by_lower[name] for name in ("time", "datetime", "timestamp") if name in columns_by_lower),
        None,
    )
    if time_column is not None:
        parsed = pd.to_datetime(raw[time_column], errors="coerce", format="mixed")
        if parsed.isna().any():
            raise TailDataError("invalid_minute_timestamp")
        labels = parsed.dt.strftime("%H:%M")
        if labels.duplicated().any():
            raise TailDataError("duplicate_minute_timestamp")
        if tuple(labels) != SESSION_LABELS:
            raise TailDataError("missing_or_out_of_order_minute_timestamp")

    result = pd.DataFrame()
    for name in MINUTE_COLUMNS:
        value = pd.to_numeric(raw[columns_by_lower[name]], errors="coerce")
        result[name] = value.astype(float)

    return result.reset_index(drop=True)


def validate_minute_window(bars: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    """Fail closed only for the window consumed by the caller."""
    if len(bars) != len(SESSION_LABELS):
        raise TailDataError(f"session_bar_count:{len(bars)}")
    window = window_bars(bars, start, end)
    for name in MINUTE_COLUMNS:
        values = window[name]
        if values.isna().any() or not values.map(math.isfinite).all():
            raise TailDataError(f"non_numeric_minute_{name}")
    prices = window[["open", "high", "low", "close"]]
    if prices.le(0).any().any():
        raise TailDataError("non_positive_ohlc")
    if window[["amount", "volume"]].lt(0).any().any():
        raise TailDataError("negative_amount_or_volume")
    if (window["high"] < prices[["open", "close", "low"]].max(axis=1)).any() or (
        window["low"] > prices[["open", "close", "high"]].min(axis=1)
    ).any():
        raise TailDataError("invalid_ohlc_range")
    return window.copy()


def normalize_minute_frame(raw: pd.DataFrame) -> pd.DataFrame:
    """Strict full-session validation retained for callers that need all 240 bars."""
    result = parse_minute_frame(raw)
    validate_minute_window(result, "09:30", "15:00")
    return result


def locate_day_source(minute_root: str | Path, trade_date: str) -> tuple[Path, str]:
    """Resolve one known trade day without enumerating another day or year."""
    key = str(trade_date).replace("-", "")
    if len(key) != 8 or not key.isdigit():
        raise TailDataError(f"invalid_trade_date:{trade_date}")
    month_root = Path(minute_root) / key[:4] / key[4:6]
    archive = month_root / f"{key}.zip"
    directory = month_root / key
    # Recent archives legitimately retain both forms.  Prefer the immutable ZIP
    # deterministically; a directory remains the fallback for directory-only days.
    if archive.exists():
        return archive, "zip"
    if directory.exists():
        return directory, "directory"
    raise TailDataError(f"minute_day_missing:{key}")


def _matching_member(names: Iterable[str], code: str) -> str:
    normalized = canonical_code(code)
    allowed_stems = {normalized, f"sh{normalized}", f"sz{normalized}"}
    matches = sorted(
        name
        for name in names
        if not name.endswith("/")
        and PurePosixPath(name).suffix.lower() == ".csv"
        and PurePosixPath(name).stem.lower() in allowed_stems
    )
    if not matches:
        raise TailDataError(f"minute_code_missing:{normalized}")
    if len(matches) != 1:
        raise TailDataError(f"ambiguous_minute_code_file:{normalized}")
    return matches[0]


def load_minute_session(source: str | Path, code: str) -> pd.DataFrame:
    """Load one code from either a daily ZIP or a daily directory, read-only."""
    path = Path(source)
    if path.is_file() and path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as archive:
            member = _matching_member(archive.namelist(), code)
            payload = archive.read(member)
    elif path.is_dir():
        candidates = sorted(
            item
            for item in path.rglob("*.csv")
            if item.stem.lower() in {canonical_code(code), f"sh{canonical_code(code)}", f"sz{canonical_code(code)}"}
        )
        if not candidates:
            raise TailDataError(f"minute_code_missing:{canonical_code(code)}")
        if len(candidates) != 1:
            raise TailDataError(f"ambiguous_minute_code_file:{canonical_code(code)}")
        payload = candidates[0].read_bytes()
    else:
        raise TailDataError(f"unsupported_minute_source:{path}")
    try:
        raw = pd.read_csv(io.BytesIO(payload))
    except Exception as exc:  # pandas provides the parse detail; callers only expose the reason code.
        raise TailDataError(f"minute_csv_unreadable:{canonical_code(code)}") from exc
    return parse_minute_frame(raw)


def load_day_sessions(minute_root: str | Path, trade_date: str, codes: Iterable[str]) -> tuple[dict[str, pd.DataFrame], dict[str, Any]]:
    source, source_kind = locate_day_source(minute_root, trade_date)
    result: dict[str, pd.DataFrame] = {}
    errors: dict[str, str] = {}
    for code in codes:
        normalized = canonical_code(code)
        try:
            result[normalized] = load_minute_session(source, normalized)
        except TailDataError as exc:
            errors[normalized] = exc.reason
    return result, {
        "trade_date": str(trade_date).replace("-", ""),
        "path": str(source),
        "source_kind": source_kind,
        "size_bytes": source.stat().st_size if source.is_file() else None,
        "mtime_ns": source.stat().st_mtime_ns,
        "errors": errors,
    }


def vwap_for_window(bars: pd.DataFrame, start: str, end: str) -> dict[str, Any]:
    window = window_bars(bars, start, end)
    traded = window[window["volume"] > 0]
    amount = float(traded["amount"].sum())
    volume = float(traded["volume"].sum())
    if volume <= 0:
        return {"status": "zero_volume_window", "vwap": None, "amount": amount, "volume": volume}
    if (traded["amount"] <= 0).any():
        return {"status": "invalid_positive_volume_amount", "vwap": None, "amount": amount, "volume": volume}
    return {"status": "ready", "vwap": amount / volume, "amount": amount, "volume": volume}


def listing_evidence(daily_root: str | Path, code: str, trade_date: str, minute_visible_sessions: int) -> dict[str, Any]:
    """Read daily-K listing evidence only; it never supplies same-day feature amounts."""
    path = Path(daily_root) / f"{canonical_code(code)}.xlsx"
    key = pd.Timestamp(str(trade_date))
    if not path.exists():
        return {
            "listing_age_source": "minute_source_visible_history",
            "listing_history_sessions": int(minute_visible_sessions),
            "daily_amount": None,
            "daily_columns": [],
        }
    try:
        daily = pd.read_excel(path, usecols=lambda column: str(column).strip().lower() in {"date", "amount"})
    except Exception as exc:
        raise TailDataError(f"daily_k_unreadable:{canonical_code(code)}") from exc
    columns = {str(column).strip().lower(): column for column in daily.columns}
    if "date" not in columns:
        raise TailDataError(f"daily_k_date_missing:{canonical_code(code)}")
    dates = pd.to_datetime(daily[columns["date"]], errors="coerce")
    valid_dates = dates.dropna()
    amount = pd.to_numeric(daily[columns["amount"]], errors="coerce") if "amount" in columns else pd.Series(dtype=float)
    matching_amount = amount[dates.eq(key)].dropna() if not amount.empty else pd.Series(dtype=float)
    return {
        "listing_age_source": "daily_k_first_date",
        "listing_first_date": valid_dates.min().date().isoformat() if not valid_dates.empty else None,
        "listing_history_sessions": int(dates.lt(key).sum()),
        "daily_amount": float(matching_amount.iloc[-1]) if not matching_amount.empty else None,
        "daily_columns": sorted(str(column) for column in daily.columns),
    }


def corporate_action_state(_daily_columns: Iterable[str]) -> str:
    """No documented, verified factor contract is available in TNM-1."""
    return "unproven_not_applied"


def _empty_feature_snapshot(code: str, reason: str, listing: Mapping[str, Any], d1_amount: float | None = None) -> dict[str, Any]:
    return {
        "sec_code": canonical_code(code),
        "feature_status": "isolated",
        "feature_reason": reason,
        "raw_features": {},
        "features": {},
        "d1_amount": d1_amount,
        "listing_age_source": listing.get("listing_age_source"),
        "listing_history_sessions": listing.get("listing_history_sessions"),
        "st_filter_applied": False,
        "st_status": "unavailable_not_filtered",
        "corporate_action_filter": corporate_action_state(listing.get("daily_columns", [])),
    }


def feature_snapshot(
    code: str,
    day_bars: pd.DataFrame,
    previous_three_sessions: Iterable[pd.DataFrame],
    listing: Mapping[str, Any],
) -> dict[str, Any]:
    """Build the six raw D-14:50 features without receiving D+1 data."""
    prior = list(previous_three_sessions)
    if not is_a_share_code(code):
        return _empty_feature_snapshot(code, "non_a_share_prefix", listing)
    if len(prior) != 3:
        return _empty_feature_snapshot(code, "missing_d3_to_d1_sessions", listing)
    try:
        for session in prior:
            validate_minute_window(session, "09:30", "15:00")
        validate_minute_window(day_bars, "09:30", "14:50")
    except TailDataError as exc:
        return _empty_feature_snapshot(code, exc.reason, listing)
    if int(listing.get("listing_history_sessions") or 0) < MIN_HISTORY_SESSIONS:
        return _empty_feature_snapshot(code, "listing_history_under_20", listing)

    d1_amount = float(prior[-1]["amount"].sum())
    if not d1_amount > MIN_D1_AMOUNT:
        return _empty_feature_snapshot(code, "d1_amount_not_strictly_above_500m", listing, d1_amount)

    tail = window_bars(day_bars, "14:20", "14:50")
    tail_first = tail.iloc[:15]
    tail_last = tail.iloc[15:]
    day_before_tail = window_bars(day_bars, "09:30", "14:20")
    prior_same_window_amounts = [float(window_bars(session, "09:30", "14:50")["amount"].sum()) for session in prior]
    tail_range = float(tail["high"].max() - tail["low"].min())
    if tail_range <= 0:
        return _empty_feature_snapshot(code, "zero_tail_price_range", listing, d1_amount)
    first_tail_amount = float(tail_first["amount"].sum())
    if first_tail_amount <= 0:
        return _empty_feature_snapshot(code, "zero_tail_amount_baseline", listing, d1_amount)
    activity_denominator = float(pd.Series(prior_same_window_amounts).median())
    if activity_denominator <= 0:
        return _empty_feature_snapshot(code, "zero_activity_history_amount", listing, d1_amount)

    d3_close = float(prior[0].iloc[-1]["close"])
    d1_close = float(prior[-1].iloc[-1]["close"])
    raw_features = {
        "tail_return": float(tail.iloc[-1]["close"] / tail.iloc[0]["open"] - 1.0),
        "tail_end_location": float((tail.iloc[-1]["close"] - tail["low"].min()) / tail_range),
        "tail_amount_accel": float(tail_last["amount"].sum() / first_tail_amount),
        "pre_tail_return": float(day_before_tail.iloc[-1]["close"] / day_bars.iloc[0]["open"] - 1.0),
        "activity_ratio": float(window_bars(day_bars, "09:30", "14:50")["amount"].sum() / activity_denominator),
        "recent_3date_return": float(d1_close / d3_close - 1.0),
    }
    return {
        "sec_code": canonical_code(code),
        "feature_status": "ready",
        "feature_reason": None,
        "raw_features": raw_features,
        "features": {},
        "d1_amount": d1_amount,
        "listing_age_source": listing.get("listing_age_source"),
        "listing_history_sessions": listing.get("listing_history_sessions"),
        "st_filter_applied": False,
        "st_status": "unavailable_not_filtered",
        "corporate_action_filter": corporate_action_state(listing.get("daily_columns", [])),
    }


def add_market_relative_features(snapshots: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Apply only same-day qualified-cross-section medians to the three relative features."""
    rows = [dict(snapshot) for snapshot in snapshots]
    qualified = [row for row in rows if row.get("feature_status") == "ready"]
    if not qualified:
        return rows
    medians = {
        source: float(pd.Series([row["raw_features"][source] for row in qualified]).median())
        for source in ("tail_return", "pre_tail_return", "recent_3date_return")
    }
    for row in qualified:
        raw = row["raw_features"]
        row["features"] = {
            "tail_return_rel": raw["tail_return"] - medians["tail_return"],
            "tail_end_location": raw["tail_end_location"],
            "tail_amount_accel": raw["tail_amount_accel"],
            "pre_tail_return_rel": raw["pre_tail_return"] - medians["pre_tail_return"],
            "activity_ratio": raw["activity_ratio"],
            "recent_3date_return_rel": raw["recent_3date_return"] - medians["recent_3date_return"],
        }
    return rows


def _unavailable_outcome(status: str, buy: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "outcome_status": status,
        "buy": buy,
        "sell": None,
        "gross_return": None,
        "net_return_15bps": None,
        "net_return_30bps": None,
        "mfe_0930_1000": None,
        "mae_0930_1000": None,
    }


def outcome_snapshot(day_bars: pd.DataFrame | None, next_day_bars: pd.DataFrame | None) -> dict[str, Any]:
    """Compute labels and executable VWAPs; this path never feeds feature eligibility."""
    if day_bars is None:
        return _unavailable_outcome("unavailable_day_session")
    try:
        validate_minute_window(day_bars, "14:51", "14:56")
    except TailDataError:
        return _unavailable_outcome("unavailable_buy_window_invalid")
    buy = vwap_for_window(day_bars, "14:51", "14:56")
    if buy["status"] != "ready":
        return _unavailable_outcome("cash_unfilled_buy", buy)
    if next_day_bars is None:
        return _unavailable_outcome("unavailable_next_day_session", buy)
    try:
        validate_minute_window(next_day_bars, "09:30", "10:05")
    except TailDataError:
        return _unavailable_outcome("unavailable_next_day_window_invalid", buy)
    sell = vwap_for_window(next_day_bars, "10:00", "10:05")
    potential = window_bars(next_day_bars, "09:30", "10:00")
    entry = float(buy["vwap"])
    mfe = float(potential["high"].max() / entry - 1.0)
    mae = float(potential["low"].min() / entry - 1.0)
    if sell["status"] != "ready":
        return {
            "outcome_status": "delayed_exit_required",
            "buy": buy,
            "sell": sell,
            "gross_return": None,
            "net_return_15bps": None,
            "net_return_30bps": None,
            "mfe_0930_1000": mfe,
            "mae_0930_1000": mae,
        }
    gross = float(float(sell["vwap"]) / entry - 1.0)
    return {
        "outcome_status": "ready",
        "buy": buy,
        "sell": sell,
        "gross_return": gross,
        "net_return_15bps": gross - 0.0015,
        "net_return_30bps": gross - 0.0030,
        "mfe_0930_1000": mfe,
        "mae_0930_1000": mae,
    }


def rank_scored_rows(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Stable generic ranking helper; TNM-1 does not create a research score."""
    ordered = sorted((dict(row) for row in rows), key=lambda row: (-float(row["score"]), str(row["sec_code"])))
    for rank, row in enumerate(ordered, start=1):
        row["rank"] = rank
    return ordered


def capacity_diagnostic(ranked_rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Diagnose fixed Top-10 capacity without changing the supplied rank or score."""
    selected = [dict(row) for row in ranked_rows if int(row.get("rank", 0)) <= 10]
    if len(selected) != 10:
        return {
            "capacity_verdict_5m": "capacity_unproven",
            "reason": "requires_frozen_top10",
            "selected_count": len(selected),
            "total_capital_yuan": CAPACITY_TOTAL_YUAN,
            "per_stock_yuan": CAPACITY_PER_STOCK_YUAN,
        }
    buy_rates: list[float] = []
    sell_rates: list[float] = []
    for row in selected:
        outcome = row.get("outcome") or {}
        buy = (outcome.get("buy") or {}).get("amount")
        sell = (outcome.get("sell") or {}).get("amount")
        if not isinstance(buy, (int, float)) or not isinstance(sell, (int, float)) or buy <= 0 or sell <= 0:
            return {
                "capacity_verdict_5m": "capacity_unproven",
                "reason": "missing_executable_buy_or_sell_window",
                "selected_count": len(selected),
                "total_capital_yuan": CAPACITY_TOTAL_YUAN,
                "per_stock_yuan": CAPACITY_PER_STOCK_YUAN,
            }
        buy_rates.append(CAPACITY_PER_STOCK_YUAN / float(buy))
        sell_rates.append(CAPACITY_PER_STOCK_YUAN / float(sell))
    status = "capacity_pass" if max(buy_rates + sell_rates) <= CAPACITY_PARTICIPATION_LIMIT else "capacity_constrained"
    return {
        "capacity_verdict_5m": status,
        "selected_count": len(selected),
        "total_capital_yuan": CAPACITY_TOTAL_YUAN,
        "per_stock_yuan": CAPACITY_PER_STOCK_YUAN,
        "buy_participation_max": max(buy_rates),
        "sell_participation_max": max(sell_rates),
        "deployability_limit": CAPACITY_PARTICIPATION_LIMIT,
    }


def _full_day_minute_amount(bars: pd.DataFrame) -> tuple[float | None, str | None]:
    values = pd.to_numeric(bars["amount"], errors="coerce")
    if values.isna().any() or not values.map(math.isfinite).all():
        return None, "minute_amount_non_finite"
    if values.lt(0).any():
        return None, "minute_amount_negative"
    return float(values.sum()), None


def daily_minute_amount_ratio(daily_amount: float | None, bars: pd.DataFrame) -> float | None:
    minute_amount, reason = _full_day_minute_amount(bars)
    if reason is not None or daily_amount is None or not math.isfinite(float(daily_amount)) or minute_amount is None or minute_amount <= 0:
        return None
    return float(daily_amount) / minute_amount


def daily_minute_crosscheck(daily_amount: float | None, bars: pd.DataFrame) -> dict[str, Any]:
    minute_amount, minute_reason = _full_day_minute_amount(bars)
    if minute_reason is not None:
        return {"status": "invalid", "reason": minute_reason, "ratio": None}
    if daily_amount is None or not math.isfinite(float(daily_amount)):
        return {"status": "unavailable", "reason": "daily_amount_missing_or_invalid", "ratio": None}
    if minute_amount is None or minute_amount <= 0:
        return {"status": "unavailable", "reason": "minute_amount_zero", "ratio": None}
    ratio = float(daily_amount) / minute_amount
    status = "ready" if abs(ratio - 1.0) <= DAILY_MINUTE_AMOUNT_RATIO_TOLERANCE else "mismatch"
    return {"status": status, "ratio": ratio}


def _clock_label(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _five_minute_grid(start_minutes: int, end_minutes: int) -> tuple[tuple[str, str], ...]:
    return tuple((_clock_label(value), _clock_label(value + 5)) for value in range(start_minutes, end_minutes, 5))


DELAYED_FIRST_DAY_WINDOWS = _five_minute_grid(10 * 60 + 5, 11 * 60 + 30) + _five_minute_grid(13 * 60, 15 * 60)
DELAYED_LATER_DAY_WINDOWS = _five_minute_grid(9 * 60 + 30, 11 * 60 + 30) + _five_minute_grid(13 * 60, 15 * 60)


def _window_vwap_statistics(bars: pd.DataFrame, start: str, end: str) -> dict[str, Any]:
    try:
        validate_minute_window(bars, start, end)
    except TailDataError as exc:
        return {"status": "invalid_window", "reason": exc.reason, "vwap": None, "amount": None, "volume": None}
    return vwap_for_window(bars, start, end)


def minute_sufficient_statistics(raw: pd.DataFrame) -> dict[str, Any]:
    """Compress one 240-bar file into only the windows consumed by TNM-2.

    The result deliberately has no full bar array.  Signal, buy, next-morning and
    delayed-exit validations remain separate so later windows cannot invalidate a
    D-14:50 feature snapshot.
    """
    try:
        bars = parse_minute_frame(raw)
    except TailDataError as exc:
        return {"status": "invalid", "reason": exc.reason}

    values = bars.loc[:, MINUTE_COLUMNS].to_numpy(dtype=float, copy=False)
    finite = np.isfinite(values)
    prices = values[:, :4]
    row_valid = (
        finite.all(axis=1)
        & (prices > 0).all(axis=1)
        & (values[:, 4:] >= 0).all(axis=1)
        & (prices[:, 1] >= np.maximum.reduce((prices[:, 0], prices[:, 2], prices[:, 3])))
        & (prices[:, 2] <= np.minimum.reduce((prices[:, 0], prices[:, 1], prices[:, 3])))
    )

    def reason_for(start: int, end: int) -> str | None:
        invalid = np.flatnonzero(~row_valid[start:end])
        if not len(invalid):
            return None
        row = start + int(invalid[0])
        for index, name in enumerate(MINUTE_COLUMNS):
            if not finite[row, index]:
                return f"non_numeric_minute_{name}"
        if (prices[row] <= 0).any():
            return "non_positive_ohlc"
        if (values[row, 4:] < 0).any():
            return "negative_amount_or_volume"
        return "invalid_ohlc_range"

    def compact_vwap(start: str, end: str) -> dict[str, Any]:
        start_index, end_index = _window_index(start), _window_index(end)
        reason = reason_for(start_index, end_index)
        if reason is not None:
            return {"status": "invalid_window", "reason": reason, "vwap": None, "amount": None, "volume": None}
        window = values[start_index:end_index]
        traded = window[:, 5] > 0
        amount = float(window[traded, 4].sum())
        volume = float(window[traded, 5].sum())
        if volume <= 0:
            return {"status": "zero_volume_window", "vwap": None, "amount": amount, "volume": volume}
        if (window[traded, 4] <= 0).any():
            return {"status": "invalid_positive_volume_amount", "vwap": None, "amount": amount, "volume": volume}
        return {"status": "ready", "vwap": amount / volume, "amount": amount, "volume": volume}

    result: dict[str, Any] = {"status": "ready"}
    full_reason = reason_for(0, len(SESSION_LABELS))
    if full_reason is None:
        result["history"] = {
            "status": "ready",
            "close": float(values[-1, 3]),
            "full_amount": float(values[:, 4].sum()),
            "pre_1450_amount": float(values[:_window_index("14:50"), 4].sum()),
        }
    else:
        result["history"] = {"status": "invalid", "reason": full_reason}
    signal_end = _window_index("14:50")
    signal_reason = reason_for(0, signal_end)
    if signal_reason is None:
        tail = values[_window_index("14:20"):signal_end]
        result["signal"] = {
            "status": "ready",
            "tail_open": float(tail[0, 0]),
            "tail_close": float(tail[-1, 3]),
            "tail_low": float(tail[:, 2].min()),
            "tail_high": float(tail[:, 1].max()),
            "tail_first_amount": float(tail[:15, 4].sum()),
            "tail_last_amount": float(tail[15:, 4].sum()),
            "pre_tail_open": float(values[0, 0]),
            "pre_tail_close": float(values[_window_index("14:20") - 1, 3]),
            "activity_amount": float(values[:signal_end, 4].sum()),
        }
    else:
        result["signal"] = {"status": "invalid", "reason": signal_reason}
    result["buy"] = compact_vwap("14:51", "14:56")
    morning_end = _window_index("10:05")
    morning_reason = reason_for(0, morning_end)
    if morning_reason is None:
        potential = values[:_window_index("10:00")]
        result["morning"] = {
            "status": "ready",
            "mfe_high": float(potential[:, 1].max()),
            "mae_low": float(potential[:, 2].min()),
            "sell": compact_vwap("10:00", "10:05"),
        }
    else:
        result["morning"] = {"status": "invalid", "reason": morning_reason, "sell": None}
    result["delayed_exit_windows"] = [{"start": start, "end": end, **compact_vwap(start, end)} for start, end in DELAYED_LATER_DAY_WINDOWS]
    return result


def _statistics_from_payload(payload: bytes, code: str) -> dict[str, Any]:
    try:
        raw = pd.read_csv(io.BytesIO(payload))
    except Exception as exc:
        return {"status": "invalid", "reason": f"minute_csv_unreadable:{canonical_code(code)}"}
    return minute_sufficient_statistics(raw)


def _code_from_csv_name(name: str) -> str:
    return canonical_code(PurePosixPath(name).stem)


def load_day_statistics(
    minute_root: str | Path,
    trade_date: str,
    *,
    allowed_years: Iterable[str] = DEVELOPMENT_YEARS,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Read every A-share file in one daily container with exactly one ZIP open."""
    key = str(trade_date).replace("-", "")
    if key[:4] not in set(str(year) for year in allowed_years):
        raise TailDataError(f"development_year_guard:{key}")
    source, source_kind = locate_day_source(minute_root, key)
    stats: dict[str, dict[str, Any]] = {}
    member_errors: dict[str, str] = {}
    if source_kind == "zip":
        with zipfile.ZipFile(source) as archive:
            members: dict[str, str] = {}
            for name in sorted(archive.namelist()):
                if name.endswith("/") or PurePosixPath(name).suffix.lower() != ".csv":
                    continue
                code = _code_from_csv_name(name)
                if not is_a_share_code(code):
                    continue
                if code in members:
                    member_errors[code] = "ambiguous_minute_code_file"
                else:
                    members[code] = name
            for code, name in sorted(members.items()):
                if code in member_errors:
                    continue
                stats[code] = _statistics_from_payload(archive.read(name), code)
        open_count = 1
    else:
        members = {}
        for item in sorted(source.rglob("*.csv")):
            code = _code_from_csv_name(item.name)
            if not is_a_share_code(code):
                continue
            if code in members:
                member_errors[code] = "ambiguous_minute_code_file"
            else:
                members[code] = item
        for code, item in sorted(members.items()):
            if code in member_errors:
                continue
            stats[code] = _statistics_from_payload(item.read_bytes(), code)
        open_count = 1
    for code, reason in member_errors.items():
        stats[code] = {"status": "invalid", "reason": reason}
    source_stat = source.stat()
    return stats, {
        "trade_date": key,
        "path": str(source),
        "source_kind": source_kind,
        "size_bytes": source_stat.st_size if source.is_file() else None,
        "mtime_ns": source_stat.st_mtime_ns,
        "container_open_count": open_count,
        "universe_count": len(stats),
        "invalid_file_count": sum(1 for value in stats.values() if value.get("status") != "ready"),
    }


def _listing_base(daily_root: str | Path, code: str, cache: dict[str, dict[str, Any]]) -> dict[str, Any]:
    normalized = canonical_code(code)
    if normalized in cache:
        return cache[normalized]
    path = Path(daily_root) / f"{normalized}.xlsx"
    if not path.exists():
        value = {"status": "missing", "daily_columns": [], "listing_first_date": None}
    else:
        try:
            from openpyxl import load_workbook

            workbook = load_workbook(path, read_only=True, data_only=True)
            rows = workbook.active.iter_rows(values_only=True)
            header = next(rows, None)
            first = next(rows, None)
            workbook.close()
            columns = [str(column) for column in (header or ())]
            date_index = next((index for index, column in enumerate(columns) if column.strip().lower() == "date"), None)
            first_date = pd.to_datetime(first[date_index], errors="coerce") if first is not None and date_index is not None else pd.NaT
            value = {
                "status": "ready" if date_index is not None and not pd.isna(first_date) else "unreadable",
                "daily_columns": columns,
                "listing_first_date": None if pd.isna(first_date) else first_date.date().isoformat(),
            }
        except Exception:
            value = {"status": "unreadable", "daily_columns": [], "listing_first_date": None}
    cache[normalized] = value
    return value


def development_listing_evidence(
    daily_root: str | Path,
    code: str,
    trade_date: str,
    minute_visible_sessions: int,
    cache: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Use only daily-K first-date/header evidence; never read K-line outcomes or amount."""
    base = _listing_base(daily_root, code, cache)
    first_date = base.get("listing_first_date")
    if base.get("status") == "ready" and first_date is not None:
        age_days = (pd.Timestamp(str(trade_date)) - pd.Timestamp(first_date)).days
        # A sixty-calendar-day lower bound is deliberately conservative for the
        # twenty-session gate and consumes no post-listing daily K-line values.
        history = MIN_HISTORY_SESSIONS if age_days >= 60 else min(int(minute_visible_sessions), MIN_HISTORY_SESSIONS - 1)
        return {
            "listing_age_source": "daily_k_first_date",
            "listing_history_sessions": history,
            "listing_first_date": first_date,
            "daily_columns": base["daily_columns"],
            "daily_amount": None,
        }
    return {
        "listing_age_source": "minute_source_visible_history",
        "listing_history_sessions": int(minute_visible_sessions),
        "listing_first_date": None,
        "daily_columns": base.get("daily_columns", []),
        "daily_amount": None,
    }


def feature_snapshot_from_statistics(
    code: str,
    day: Mapping[str, Any] | None,
    previous_three: Iterable[Mapping[str, Any] | None],
    listing: Mapping[str, Any],
) -> dict[str, Any]:
    """The TNM-1 six-feature formula over day-level sufficient statistics."""
    prior = list(previous_three)
    if not is_a_share_code(code):
        return _empty_feature_snapshot(code, "non_a_share_prefix", listing)
    if day is None or len(prior) != 3 or any(value is None for value in prior):
        return _empty_feature_snapshot(code, "missing_d3_to_d1_sessions", listing)
    if any(value.get("history", {}).get("status") != "ready" for value in prior):
        reason = next(value.get("history", {}).get("reason", "historical_session_invalid") for value in prior if value.get("history", {}).get("status") != "ready")
        return _empty_feature_snapshot(code, reason, listing)
    signal = day.get("signal", {})
    if signal.get("status") != "ready":
        return _empty_feature_snapshot(code, signal.get("reason", "signal_session_invalid"), listing)
    if int(listing.get("listing_history_sessions") or 0) < MIN_HISTORY_SESSIONS:
        return _empty_feature_snapshot(code, "listing_history_under_20", listing)
    d1_amount = float(prior[-1]["history"]["full_amount"])
    if not d1_amount > MIN_D1_AMOUNT:
        return _empty_feature_snapshot(code, "d1_amount_not_strictly_above_500m", listing, d1_amount)
    tail_range = float(signal["tail_high"] - signal["tail_low"])
    if tail_range <= 0:
        return _empty_feature_snapshot(code, "zero_tail_price_range", listing, d1_amount)
    if float(signal["tail_first_amount"]) <= 0:
        return _empty_feature_snapshot(code, "zero_tail_amount_baseline", listing, d1_amount)
    history_amounts = [float(value["history"]["pre_1450_amount"]) for value in prior]
    activity_denominator = float(pd.Series(history_amounts).median())
    if activity_denominator <= 0:
        return _empty_feature_snapshot(code, "zero_activity_history_amount", listing, d1_amount)
    raw_features = {
        "tail_return": float(signal["tail_close"] / signal["tail_open"] - 1.0),
        "tail_end_location": float((signal["tail_close"] - signal["tail_low"]) / tail_range),
        "tail_amount_accel": float(signal["tail_last_amount"] / signal["tail_first_amount"]),
        "pre_tail_return": float(signal["pre_tail_close"] / signal["pre_tail_open"] - 1.0),
        "activity_ratio": float(signal["activity_amount"] / activity_denominator),
        "recent_3date_return": float(prior[-1]["history"]["close"] / prior[0]["history"]["close"] - 1.0),
    }
    return {
        "sec_code": canonical_code(code),
        "feature_status": "ready",
        "feature_reason": None,
        "raw_features": raw_features,
        "features": {},
        "d1_amount": d1_amount,
        "listing_age_source": listing.get("listing_age_source"),
        "listing_history_sessions": listing.get("listing_history_sessions"),
        "st_filter_applied": False,
        "st_status": "unavailable_not_filtered",
        "corporate_action_filter": corporate_action_state(listing.get("daily_columns", [])),
    }


def _completed_outcome(buy: Mapping[str, Any], sell: Mapping[str, Any], mfe: float | None, mae: float | None, exit_date: str) -> dict[str, Any]:
    gross = float(float(sell["vwap"]) / float(buy["vwap"]) - 1.0)
    return {
        "outcome_status": "ready",
        "buy": dict(buy),
        "sell": dict(sell),
        "gross_return": gross,
        "net_return_15bps": gross - 0.0015,
        "net_return_30bps": gross - 0.0030,
        "mfe_0930_1000": mfe,
        "mae_0930_1000": mae,
        "exit_date": exit_date,
    }


def outcome_from_statistics(day: Mapping[str, Any] | None, next_day: Mapping[str, Any] | None, next_date: str) -> dict[str, Any]:
    """Outcome-only execution state, including an explicit delayed-exit state."""
    if day is None:
        return _unavailable_outcome("unavailable_day_session")
    buy = day.get("buy", {})
    if buy.get("status") == "invalid_window":
        return _unavailable_outcome("unavailable_buy_window_invalid")
    if buy.get("status") != "ready":
        return _unavailable_outcome("cash_unfilled_buy", dict(buy))
    if next_day is None or next_day.get("morning", {}).get("status") != "ready":
        return {
            **_unavailable_outcome("delayed_exit_required", dict(buy)),
            "outcome_status": "delayed_exit_required",
            "exit_date": None,
        }
    morning = next_day["morning"]
    mfe = float(morning["mfe_high"] / float(buy["vwap"]) - 1.0)
    mae = float(morning["mae_low"] / float(buy["vwap"]) - 1.0)
    sell = morning["sell"]
    if sell.get("status") == "ready":
        return _completed_outcome(buy, sell, mfe, mae, next_date)
    return {
        "outcome_status": "delayed_exit_required",
        "buy": dict(buy),
        "sell": dict(sell),
        "gross_return": None,
        "net_return_15bps": None,
        "net_return_30bps": None,
        "mfe_0930_1000": mfe,
        "mae_0930_1000": mae,
        "exit_date": None,
    }


def resolve_delayed_exit(
    outcome: Mapping[str, Any],
    day_statistics: Mapping[str, Any] | None,
    trade_date: str,
    target_date: str,
    *,
    first_delayed_day: bool,
) -> dict[str, Any] | None:
    """Return the first executable fixed-grid exit, never crossing the target year."""
    if str(trade_date)[:4] != str(target_date)[:4]:
        return None
    if day_statistics is None or outcome.get("outcome_status") != "delayed_exit_required":
        return None
    allowed = set(DELAYED_FIRST_DAY_WINDOWS if first_delayed_day else DELAYED_LATER_DAY_WINDOWS)
    for window in day_statistics.get("delayed_exit_windows", []):
        if (window.get("start"), window.get("end")) not in allowed or window.get("status") != "ready":
            continue
        return _completed_outcome(
            outcome["buy"],
            window,
            outcome.get("mfe_0930_1000"),
            outcome.get("mae_0930_1000"),
            str(trade_date),
        )
    return None


def _canonical_json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def _json_digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
        handle.write(payload)
        temporary = Path(handle.name)
    os.replace(temporary, path)


def _atomic_write_json(path: Path, value: Any) -> None:
    _atomic_write_bytes(path, _canonical_json_bytes(value))


def _current_git_commit() -> str:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        return completed.stdout.strip()
    except Exception:
        return "git_commit_unavailable"


def _runner_source_hash() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _frozen_contract_hash(task_card: str | Path) -> str:
    """Hash only the frozen task definition, never mutable execution evidence."""
    marker = "## 11. 执行结果".encode("utf-8")
    definition, separator, _execution_results = Path(task_card).read_bytes().partition(marker)
    if not separator:
        raise TailDataError("frozen_contract_execution_results_marker_missing")
    return hashlib.sha256(definition).hexdigest()


def _spec_hash() -> str:
    task_card = Path(__file__).resolve().parents[1] / "docs" / "superpowers" / "specs" / "2026-08-24-tail-next-morning-v1-task-cards.md"
    return _frozen_contract_hash(task_card)


def _discover_trade_dates(minute_root: str | Path, years: Iterable[str]) -> list[str]:
    root = Path(minute_root)
    result: set[str] = set()
    for year in sorted(set(str(year) for year in years)):
        year_root = root / year
        if not year_root.is_dir():
            continue
        for month_root in sorted(item for item in year_root.iterdir() if item.is_dir()):
            for item in month_root.iterdir():
                name = item.stem if item.is_file() and item.suffix.lower() == ".zip" else item.name
                if len(name) != 8 or not name.isdigit() or not name.startswith(year):
                    continue
                if item.is_dir() or (item.is_file() and item.suffix.lower() == ".zip"):
                    result.add(name)
    return sorted(result)


def _target_dates_for_development(calendar: list[str], *, canary: bool) -> tuple[list[str], list[str]]:
    targets = [
        calendar[index]
        for index in range(3, len(calendar) - 1)
        if calendar[index][:4] == calendar[index + 1][:4] and calendar[index][:4] in DEVELOPMENT_YEARS
    ]
    if canary:
        targets = [date for date in targets if date.startswith("2023")][:10]
        if len(targets) != 10:
            raise TailDataError("canary_requires_ten_2023_targets")
        last = calendar.index(targets[-1]) + 1
        return targets, calendar[: last + 1]
    return targets, calendar


def _input_manifest(minute_root: str | Path, trade_dates: Iterable[str], allowed_years: Iterable[str]) -> list[dict[str, Any]]:
    allowed = set(str(year) for year in allowed_years)
    records: list[dict[str, Any]] = []
    for trade_date in sorted(set(trade_dates)):
        if str(trade_date)[:4] not in allowed:
            raise TailDataError(f"development_year_guard:{trade_date}")
        source, source_kind = locate_day_source(minute_root, trade_date)
        stat = source.stat()
        records.append(
            {
                "trade_date": str(trade_date),
                "path": str(source),
                "source_kind": source_kind,
                "size_bytes": stat.st_size if source.is_file() else None,
                "mtime_ns": stat.st_mtime_ns,
            }
        )
    return records


def _prepare_development_run(
    output_dir: str | Path,
    *,
    mode: str,
    input_manifest: list[dict[str, Any]],
    resume_run_id: str | None = None,
) -> tuple[Path, dict[str, Any]]:
    manifest_hash = _json_digest(input_manifest)
    identity = {
        "mode": mode,
        "spec_hash": _spec_hash(),
        "code_commit": _current_git_commit(),
        "runner_source_hash": _runner_source_hash(),
        "input_manifest_hash": manifest_hash,
    }
    run_hash = _json_digest(identity)
    calculated_run_id = f"{mode}-{run_hash[:16]}"
    run_id = resume_run_id or calculated_run_id
    run_dir = Path(output_dir) / run_id
    run_manifest = {**identity, "run_hash": run_hash, "run_id": run_id, "input_manifest": input_manifest}
    existing = run_dir / "run_manifest.json"
    if existing.exists():
        recorded = json.loads(existing.read_text(encoding="utf-8"))
        if recorded.get("run_hash") != run_hash or recorded.get("input_manifest_hash") != manifest_hash:
            raise TailDataError("resume_hash_mismatch")
    else:
        _atomic_write_json(existing, run_manifest)
    return run_dir, run_manifest


def _acquire_run_lock(run_dir: Path, run_id: str) -> Path:
    lock = run_dir / "run.lock"
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise TailDataError("single_writer_lock_exists") from exc
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(run_id + "\n")
    return lock


def _checkpoint_path(run_dir: Path, trade_date: str) -> Path:
    return run_dir / "checkpoints" / f"{trade_date}.json"


def _read_checkpoint(path: Path, run_hash: str) -> dict[str, Any] | None:
    if not path.exists():
        return None
    checkpoint = json.loads(path.read_text(encoding="utf-8"))
    if checkpoint.get("run_hash") != run_hash:
        raise TailDataError("checkpoint_hash_mismatch")
    return checkpoint


def _net30(outcome: Mapping[str, Any] | None) -> float | None:
    if not outcome:
        return None
    if outcome.get("outcome_status") == "cash_unfilled_buy":
        return 0.0
    value = outcome.get("net_return_30bps")
    return float(value) if isinstance(value, (int, float)) and math.isfinite(float(value)) else None


def _add_feature_percentiles(rows: list[dict[str, Any]]) -> None:
    ready = [row for row in rows if row.get("feature_status") == "ready"]
    for row in ready:
        row["feature_percentiles"] = {}
    for feature in FEATURE_NAMES:
        values = pd.Series({row["sec_code"]: row["features"][feature] for row in ready}, dtype=float)
        ranks = values.rank(method="average", pct=True)
        for row in ready:
            row["feature_percentiles"][feature] = float(ranks[row["sec_code"]])


def _fixed_ten_slots(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    selected = list(rows)[:SLOT_COUNT]
    slots: list[dict[str, Any]] = []
    gross_values: list[float] = []
    net15_values: list[float] = []
    net30_values: list[float] = []
    blocked = False
    for slot in range(SLOT_COUNT):
        if slot >= len(selected):
            slots.append({"slot": slot + 1, "sec_code": None, "gross": 0.0, "net15": 0.0, "net30": 0.0, "status": "cash_empty_slot"})
            gross_values.append(0.0)
            net15_values.append(0.0)
            net30_values.append(0.0)
            continue
        row = selected[slot]
        outcome = row.get("outcome") or {}
        net30 = _net30(outcome)
        if net30 is None:
            blocked = True
            slots.append({"slot": slot + 1, "sec_code": row["sec_code"], "gross": None, "net15": None, "net30": None, "status": "unresolved_exit"})
        elif outcome.get("outcome_status") == "cash_unfilled_buy":
            slots.append({"slot": slot + 1, "sec_code": row["sec_code"], "gross": 0.0, "net15": 0.0, "net30": 0.0, "status": "cash_unfilled_buy"})
            gross_values.append(0.0)
            net15_values.append(0.0)
            net30_values.append(0.0)
        else:
            gross = float(outcome["gross_return"])
            net15 = float(outcome["net_return_15bps"])
            slots.append({"slot": slot + 1, "sec_code": row["sec_code"], "gross": gross, "net15": net15, "net30": net30, "status": outcome["outcome_status"]})
            gross_values.append(gross)
            net15_values.append(net15)
            net30_values.append(net30)
    return {
        "slots": slots,
        "gross": None if blocked else float(sum(gross_values) / SLOT_COUNT),
        "net15": None if blocked else float(sum(net15_values) / SLOT_COUNT),
        "net30": None if blocked else float(sum(net30_values) / SLOT_COUNT),
        "blocked": blocked,
    }


def _random_top10(rows: Iterable[Mapping[str, Any]], trade_date: str) -> list[dict[str, Any]]:
    return sorted(
        (dict(row) for row in rows),
        key=lambda row: (hashlib.sha256(f"{RANDOM_BASELINE_SEED}|{trade_date}|{row['sec_code']}".encode("utf-8")).hexdigest(), row["sec_code"]),
    )[:SLOT_COUNT]


def _feature_spread(rows: list[dict[str, Any]], feature: str) -> dict[str, Any]:
    top = [row for row in rows if row["feature_percentiles"][feature] >= 0.90]
    bottom = [row for row in rows if row["feature_percentiles"][feature] <= 0.10]
    top_values = [_net30(row.get("outcome")) for row in top]
    bottom_values = [_net30(row.get("outcome")) for row in bottom]
    if not top or not bottom:
        return {"status": "unavailable", "spread": None, "top_count": len(top), "bottom_count": len(bottom)}
    if any(value is None for value in top_values + bottom_values):
        return {"status": "blocked_unresolved_exit", "spread": None, "top_count": len(top), "bottom_count": len(bottom)}
    return {
        "status": "ready",
        "spread": float(sum(top_values) / len(top_values) - sum(bottom_values) / len(bottom_values)),
        "top_count": len(top),
        "bottom_count": len(bottom),
    }


def select_development_features(daily_rows: Mapping[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """Freeze 2023 direction, use 2024 only as a same-direction veto."""
    daily: dict[str, dict[str, dict[str, Any]]] = {}
    yearly: dict[str, dict[str, list[float]]] = {year: {feature: [] for feature in FEATURE_NAMES} for year in DEVELOPMENT_YEARS}
    blocked = False
    for trade_date, rows in sorted(daily_rows.items()):
        ready = [row for row in rows if row.get("feature_status") == "ready"]
        per_feature = {feature: _feature_spread(ready, feature) for feature in FEATURE_NAMES}
        daily[trade_date] = per_feature
        for feature, result in per_feature.items():
            if result["status"] == "blocked_unresolved_exit":
                blocked = True
            if result["status"] == "ready" and trade_date[:4] in yearly:
                yearly[trade_date[:4]][feature].append(float(result["spread"]))
    annual: dict[str, dict[str, Any]] = {year: {} for year in DEVELOPMENT_YEARS}
    for year in DEVELOPMENT_YEARS:
        for feature in FEATURE_NAMES:
            values = yearly[year][feature]
            annual[year][feature] = {"available_days": len(values), "mean_spread": float(sum(values) / len(values)) if values else None}
    selected: list[dict[str, Any]] = []
    if not blocked:
        for group, members in FEATURE_GROUPS.items():
            eligible: list[tuple[float, int, str, int]] = []
            for feature in members:
                first = annual["2023"][feature]["mean_spread"]
                second = annual["2024"][feature]["mean_spread"]
                if first is None or second is None or first == 0 or second == 0 or (first > 0) != (second > 0):
                    continue
                eligible.append((abs(float(first)), -FEATURE_NUMBER[feature], feature, 1 if first > 0 else -1))
            if eligible:
                _, _, feature, direction = max(eligible)
                selected.append({"group": group, "feature": feature, "direction": direction, "spread_2023": annual["2023"][feature]["mean_spread"]})
    return {
        "daily_feature_spreads": daily,
        "annual_feature_spreads": annual,
        "selected_features": sorted(selected, key=lambda row: FEATURE_NUMBER[row["feature"]]),
        "status": "blocked_unresolved_exit" if blocked else "ready",
    }


def _portfolio_day(rows: list[dict[str, Any]], trade_date: str, directions: Mapping[str, int]) -> dict[str, Any]:
    ready = [row for row in rows if row.get("feature_status") == "ready"]
    scored = []
    for row in ready:
        score = sum(row["feature_percentiles"][feature] if direction > 0 else 1.0 - row["feature_percentiles"][feature] for feature, direction in directions.items()) / len(directions)
        scored.append({**row, "score": float(score)})
    strategy = _fixed_ten_slots(sorted(scored, key=lambda row: (-row["score"], row["sec_code"])))
    random = _fixed_ten_slots(_random_top10(ready, trade_date))
    tail = _fixed_ten_slots(sorted(ready, key=lambda row: (-row["features"]["tail_return_rel"], row["sec_code"])))
    pool_values = [_net30(row.get("outcome")) for row in ready]
    pool_blocked = any(value is None for value in pool_values)
    return {
        "trade_date": trade_date,
        "eligible_count": len(ready),
        "strategy": strategy,
        "eligible_pool_equal_weight_net30": None if pool_blocked or not pool_values else float(sum(pool_values) / len(pool_values)),
        "random_top10": random,
        "tail_return_rel_top10": tail,
    }


def _target_rows(
    target_date: str,
    next_date: str,
    prior_days: tuple[Mapping[str, Mapping[str, Any]], Mapping[str, Mapping[str, Any]], Mapping[str, Mapping[str, Any]]],
    day_statistics: Mapping[str, Mapping[str, Any]],
    next_statistics: Mapping[str, Mapping[str, Any]],
    daily_root: str | Path,
    visible_history: Mapping[str, int],
    listing_cache: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for code, day in sorted(day_statistics.items()):
        listing = development_listing_evidence(daily_root, code, target_date, int(visible_history.get(code, 0)), listing_cache)
        snapshot = feature_snapshot_from_statistics(code, day, tuple(previous.get(code) for previous in prior_days), listing)
        if snapshot["feature_status"] == "ready":
            snapshot["outcome"] = outcome_from_statistics(day, next_statistics.get(code), next_date)
            snapshot["event_id"] = f"{target_date}|{code}"
        rows.append(snapshot)
    rows = add_market_relative_features(rows)
    _add_feature_percentiles(rows)
    return rows


def _blocked_outcome(outcome: Mapping[str, Any]) -> dict[str, Any]:
    return {
        **dict(outcome),
        "outcome_status": "unresolved_exit_at_phase_end",
        "sell": None,
        "gross_return": None,
        "net_return_15bps": None,
        "net_return_30bps": None,
        "exit_date": None,
    }


def _capacity_sleeve_ledger(daily_rows: Mapping[str, list[dict[str, Any]]], directions: Mapping[str, int]) -> dict[str, Any]:
    """A separate 10x500k ledger.  It consumes rankings but never mutates them."""
    sleeves: list[dict[str, Any] | None] = [None] * SLOT_COUNT
    buys: list[float] = []
    sells: list[float] = []
    blocked = False
    filled = 0
    for trade_date, rows in sorted(daily_rows.items()):
        for index, sleeve in enumerate(sleeves):
            if sleeve is not None and sleeve.get("exit_date") is not None and str(sleeve["exit_date"]) <= trade_date:
                sleeves[index] = None
        ready = [row for row in rows if row.get("feature_status") == "ready"]
        scored = []
        for row in ready:
            score = sum(row["feature_percentiles"][feature] if direction > 0 else 1.0 - row["feature_percentiles"][feature] for feature, direction in directions.items()) / len(directions)
            scored.append({**row, "score": float(score)})
        selected = sorted(scored, key=lambda row: (-row["score"], row["sec_code"]))[:SLOT_COUNT]
        free = [index for index, sleeve in enumerate(sleeves) if sleeve is None]
        for row, sleeve_index in zip(selected, free):
            outcome = row.get("outcome") or {}
            buy = outcome.get("buy") or {}
            if buy.get("status") != "ready":
                continue
            if _net30(outcome) is None:
                blocked = True
                sleeves[sleeve_index] = {"sec_code": row["sec_code"], "exit_date": None}
                continue
            sell = outcome.get("sell") or {}
            buy_amount = buy.get("amount")
            sell_amount = sell.get("amount")
            if not isinstance(buy_amount, (int, float)) or not isinstance(sell_amount, (int, float)) or buy_amount <= 0 or sell_amount <= 0:
                blocked = True
                sleeves[sleeve_index] = {"sec_code": row["sec_code"], "exit_date": None}
                continue
            buys.append(CAPACITY_PER_STOCK_YUAN / float(buy_amount))
            sells.append(CAPACITY_PER_STOCK_YUAN / float(sell_amount))
            sleeves[sleeve_index] = {"sec_code": row["sec_code"], "exit_date": outcome.get("exit_date")}
            filled += 1
    if blocked:
        verdict = "capacity_unproven"
    elif not buys or not sells:
        verdict = "capacity_unproven"
    else:
        verdict = "capacity_pass" if max(buys + sells) <= CAPACITY_PARTICIPATION_LIMIT else "capacity_constrained"
    return {
        "capacity_verdict_5m": verdict,
        "total_capital_yuan": CAPACITY_TOTAL_YUAN,
        "per_stock_yuan": CAPACITY_PER_STOCK_YUAN,
        "sleeve_count": SLOT_COUNT,
        "filled_buy_count": filled,
        "buy_participation_max": max(buys) if buys else None,
        "sell_participation_max": max(sells) if sells else None,
        "deployability_limit": CAPACITY_PARTICIPATION_LIMIT,
        "blocked_unresolved_exit": blocked,
    }


def _write_economic_csv(path: Path, portfolios: list[dict[str, Any]]) -> None:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(
        stream,
        fieldnames=("trade_date", "eligible_count", "strategy_gross", "strategy_net15", "strategy_net30", "pool_net30", "random_net30", "tail_return_net30"),
        lineterminator="\n",
    )
    writer.writeheader()
    for row in portfolios:
        writer.writerow(
            {
                "trade_date": row["trade_date"],
                "eligible_count": row["eligible_count"],
                "strategy_gross": row["strategy"]["gross"],
                "strategy_net15": row["strategy"]["net15"],
                "strategy_net30": row["strategy"]["net30"],
                "pool_net30": row["eligible_pool_equal_weight_net30"],
                "random_net30": row["random_top10"]["net30"],
                "tail_return_net30": row["tail_return_rel_top10"]["net30"],
            }
        )
    compressed = io.BytesIO()
    with gzip.GzipFile(fileobj=compressed, mode="wb", filename="", mtime=0) as handle:
        handle.write(stream.getvalue().encode("utf-8"))
    _atomic_write_bytes(path, compressed.getvalue())


def _finalize_economic_outputs(
    run_dir: Path,
    *,
    mode: str,
    canary: bool,
    target_dates: list[str],
    daily_rows: Mapping[str, list[dict[str, Any]]],
) -> tuple[dict[str, Any], dict[str, str]]:
    selection = select_development_features(daily_rows)
    if canary:
        directions = {"tail_return_rel": 1}
        selection_status = "canary_probe_not_frozen"
    else:
        directions = {row["feature"]: int(row["direction"]) for row in selection["selected_features"]}
        selection_status = "ready" if directions and selection["status"] == "ready" else "no_stable_development_signal"
    portfolios = [_portfolio_day(daily_rows[date], date, directions) for date in target_dates] if directions else []
    capacity = _capacity_sleeve_ledger({date: daily_rows[date] for date in target_dates}, directions) if directions else {
        "capacity_verdict_5m": "capacity_unproven",
        "reason": "no_selected_features",
        "total_capital_yuan": CAPACITY_TOTAL_YUAN,
        "per_stock_yuan": CAPACITY_PER_STOCK_YUAN,
        "sleeve_count": SLOT_COUNT,
    }
    economic = {
        "contract": "TNM-2 development runner",
        "mode": mode,
        "target_dates": target_dates,
        "st_filter_applied": False,
        "st_status": "unavailable_not_filtered",
        "corporate_action_filter": "unproven_not_applied",
        "queue_model_status": "unavailable_not_modeled_vwap_small_order_baseline",
        "selection_status": selection_status,
        "feature_selection": selection,
        "portfolios": portfolios,
        "capacity_ledger": capacity,
    }
    result_path = run_dir / "development_results.json"
    csv_path = run_dir / "daily_results.csv.gz"
    _atomic_write_json(result_path, economic)
    _write_economic_csv(csv_path, portfolios)
    artifact_hashes = {
        result_path.name: hashlib.sha256(result_path.read_bytes()).hexdigest(),
        csv_path.name: hashlib.sha256(csv_path.read_bytes()).hexdigest(),
    }
    if not canary:
        frozen_rule = {
            "selected_features": selection["selected_features"],
            "selection_status": selection_status,
            "corporate_action_filter": "unproven_not_applied",
            "st_filter_applied": False,
        }
        frozen_path = run_dir / "frozen_rule.json"
        _atomic_write_json(frozen_path, frozen_rule)
        artifact_hashes[frozen_path.name] = hashlib.sha256(frozen_path.read_bytes()).hexdigest()
    _atomic_write_json(run_dir / "sha256_manifest.json", artifact_hashes)
    return economic, artifact_hashes


def run_development(
    minute_root: str | Path,
    daily_root: str | Path,
    output_dir: str | Path,
    *,
    canary: bool,
    resume_run_id: str | None = None,
) -> tuple[dict[str, Any], Path]:
    """One read-only production path for the fixed canary and later dev-run.

    `canary=True` is TNM-2A's sole executable path: exactly the first ten 2023
    targets and no frozen rule.  The `False` branch exists for TNM-2B after its
    independent approval; this task never invokes it.
    """
    allowed_years = ("2023",) if canary else DEVELOPMENT_YEARS
    calendar = _discover_trade_dates(minute_root, allowed_years)
    target_dates, process_dates = _target_dates_for_development(calendar, canary=canary)
    manifest = _input_manifest(minute_root, process_dates, allowed_years)
    mode = "dev-preflight" if canary else "dev-run"
    run_dir, run_manifest = _prepare_development_run(output_dir, mode=mode, input_manifest=manifest, resume_run_id=resume_run_id)
    lock = _acquire_run_lock(run_dir, run_manifest["run_id"])
    completion_path = run_dir / "completion.json"
    try:
        completed = json.loads(completion_path.read_text(encoding="utf-8")) if completion_path.exists() else None
        if completed and completed.get("status") == "SUCCEEDED" and completed.get("run_hash") == run_manifest["run_hash"]:
            return json.loads((run_dir / "development_results.json").read_text(encoding="utf-8")), run_dir
        _atomic_write_json(run_dir / "progress.json", {"run_id": run_manifest["run_id"], "status": "RUNNING", "completed_target_dates": 0, "total_target_dates": len(target_dates)})
        source_by_date: dict[str, dict[str, dict[str, Any]]] = {}
        source_records: dict[str, dict[str, Any]] = {}
        visible_history: dict[str, int] = {}
        listing_cache: dict[str, dict[str, Any]] = {}
        pending: dict[str, dict[str, Any]] = {}
        updates_path = run_dir / "outcome_updates.json"
        updates: dict[str, dict[str, Any]] = json.loads(updates_path.read_text(encoding="utf-8")) if updates_path.exists() else {}
        completed_targets = 0
        for index, trade_date in enumerate(process_dates):
            statistics, source_record = load_day_statistics(minute_root, trade_date, allowed_years=allowed_years)
            source_by_date[trade_date] = statistics
            source_records[trade_date] = source_record
            for event_id, event in list(pending.items()):
                if trade_date[:4] != event["target_date"][:4]:
                    updates[event_id] = _blocked_outcome(event["outcome"])
                    pending.pop(event_id)
                    continue
                resolved = resolve_delayed_exit(
                    event["outcome"],
                    statistics.get(event["sec_code"]),
                    trade_date,
                    event["target_date"],
                    first_delayed_day=trade_date == event["initial_date"],
                )
                if resolved is not None:
                    updates[event_id] = resolved
                    pending.pop(event_id)
            if index >= 4:
                target_date = process_dates[index - 1]
                if target_date in target_dates:
                    checkpoint_path = _checkpoint_path(run_dir, target_date)
                    checkpoint = _read_checkpoint(checkpoint_path, run_manifest["run_hash"])
                    if checkpoint is None:
                        rows = _target_rows(
                            target_date,
                            trade_date,
                            (source_by_date[process_dates[index - 4]], source_by_date[process_dates[index - 3]], source_by_date[process_dates[index - 2]]),
                            source_by_date[target_date],
                            statistics,
                            daily_root,
                            {code: max(0, count - 1) for code, count in visible_history.items()},
                            listing_cache,
                        )
                        checkpoint = {"run_hash": run_manifest["run_hash"], "target_date": target_date, "rows": rows, "source_record": source_records[target_date]}
                        _atomic_write_json(checkpoint_path, checkpoint)
                    for row in checkpoint["rows"]:
                        outcome = row.get("outcome") or {}
                        if outcome.get("outcome_status") == "delayed_exit_required" and row["event_id"] not in updates:
                            event = {
                                "target_date": target_date,
                                "sec_code": row["sec_code"],
                                "initial_date": trade_date,
                                "outcome": outcome,
                            }
                            resolved = resolve_delayed_exit(
                                outcome,
                                statistics.get(row["sec_code"]),
                                trade_date,
                                target_date,
                                first_delayed_day=True,
                            )
                            if resolved is None:
                                pending[row["event_id"]] = event
                            else:
                                updates[row["event_id"]] = resolved
                    completed_targets += 1
                    _atomic_write_json(
                        run_dir / "progress.json",
                        {"run_id": run_manifest["run_id"], "status": "RUNNING", "completed_target_dates": completed_targets, "total_target_dates": len(target_dates)},
                    )
            for code in statistics:
                visible_history[code] = int(visible_history.get(code, 0)) + 1
            _atomic_write_json(updates_path, updates)
            if index >= 4:
                source_by_date.pop(process_dates[index - 4], None)
        for event_id, event in pending.items():
            updates[event_id] = _blocked_outcome(event["outcome"])
        _atomic_write_json(updates_path, updates)
        daily_rows: dict[str, list[dict[str, Any]]] = {}
        for target_date in target_dates:
            checkpoint = _read_checkpoint(_checkpoint_path(run_dir, target_date), run_manifest["run_hash"])
            if checkpoint is None:
                raise TailDataError(f"checkpoint_missing:{target_date}")
            rows = checkpoint["rows"]
            for row in rows:
                event_id = row.get("event_id")
                if event_id in updates:
                    row["outcome"] = updates[event_id]
            daily_rows[target_date] = rows
        economic, hashes = _finalize_economic_outputs(run_dir, mode=mode, canary=canary, target_dates=target_dates, daily_rows=daily_rows)
        completion = {"status": "SUCCEEDED", "run_id": run_manifest["run_id"], "run_hash": run_manifest["run_hash"], "economic_artifacts": hashes}
        _atomic_write_json(completion_path, completion)
        _atomic_write_json(run_dir / "progress.json", {"run_id": run_manifest["run_id"], "status": "SUCCEEDED", "completed_target_dates": len(target_dates), "total_target_dates": len(target_dates)})
        return economic, run_dir
    except Exception as exc:
        _atomic_write_json(completion_path, {"status": "FAILED", "run_id": run_manifest["run_id"], "run_hash": run_manifest["run_hash"], "reason": getattr(exc, "reason", type(exc).__name__)})
        raise
    finally:
        if lock.exists():
            lock.unlink()


def run_dev_preflight(minute_root: str | Path, daily_root: str | Path, output_dir: str | Path, *, resume_run_id: str | None = None) -> tuple[dict[str, Any], Path]:
    return run_development(minute_root, daily_root, output_dir, canary=True, resume_run_id=resume_run_id)


PREFLIGHT_SAMPLES = (
    {
        "sample_id": "2023_zip",
        "target_date": "20230106",
        "dates": ("20230103", "20230104", "20230105", "20230106", "20230109"),
        "expected_source_kind": "zip",
    },
    {
        "sample_id": "2024_directory",
        "target_date": "20240105",
        "dates": ("20240102", "20240103", "20240104", "20240105", "20240108"),
        "expected_source_kind": "directory",
    },
    {
        "sample_id": "2026_zip",
        "target_date": "20260108",
        "dates": ("20260105", "20260106", "20260107", "20260108", "20260109"),
        "expected_source_kind": "zip",
    },
)
PREFLIGHT_CODES = ("000001", "300750", "600000")


def _preflight_sample(minute_root: Path, daily_root: Path, sample: Mapping[str, Any]) -> dict[str, Any]:
    sessions_by_day: dict[str, dict[str, pd.DataFrame]] = {}
    source_records: list[dict[str, Any]] = []
    sample_errors: list[str] = []
    for day in sample["dates"]:
        try:
            sessions, record = load_day_sessions(minute_root, day, PREFLIGHT_CODES)
        except TailDataError as exc:
            sessions = {}
            record = {"trade_date": day, "source_kind": None, "path": None, "errors": {"source": exc.reason}}
        if record.get("source_kind") != sample["expected_source_kind"]:
            sample_errors.append(f"source_kind_mismatch:{day}:{record.get('source_kind')}")
        if record.get("errors"):
            sample_errors.extend(f"{day}:{code}:{reason}" for code, reason in record["errors"].items())
        sessions_by_day[day] = sessions
        source_records.append(record)

    target = sample["target_date"]
    rows: list[dict[str, Any]] = []
    for code in PREFLIGHT_CODES:
        d3, d2, d1, day = [sessions_by_day[date].get(code) for date in sample["dates"][:4]]
        next_day = sessions_by_day[sample["dates"][4]].get(code)
        listing = listing_evidence(daily_root, code, target, minute_visible_sessions=3)
        if any(session is None for session in (d3, d2, d1, day)):
            snapshot = _empty_feature_snapshot(code, "signal_session_missing_or_invalid", listing)
        else:
            snapshot = feature_snapshot(code, day, (d3, d2, d1), listing)
        outcome = outcome_snapshot(day, next_day)
        amount_crosscheck = daily_minute_crosscheck(listing.get("daily_amount"), day) if day is not None else {"status": "unavailable", "ratio": None}
        if day is not None and amount_crosscheck["status"] != "ready":
            reason = amount_crosscheck.get("reason", "ratio_out_of_tolerance")
            sample_errors.append(f"daily_minute_amount_{amount_crosscheck['status']}:{code}:{reason}")
        rows.append(
            {
                **snapshot,
                "sample_status": "ready" if snapshot["feature_status"] == "ready" else "isolated_feature",
                "outcome": outcome,
                "daily_amount": listing.get("daily_amount"),
                "daily_minute_amount_crosscheck": amount_crosscheck,
                "daily_amount_used_for_decision": False,
            }
        )
    rows = add_market_relative_features(rows)
    return {
        "sample_id": sample["sample_id"],
        "target_date": target,
        "expected_source_kind": sample["expected_source_kind"],
        "source_records": source_records,
        "rows": rows,
        "sample_errors": sample_errors,
    }


def run_preflight(minute_root: str | Path, daily_root: str | Path, output_dir: str | Path) -> tuple[dict[str, Any], Path, str]:
    """Run exactly the frozen 3-year, 3-code, D-3-to-D+1 source preflight."""
    minute_path = Path(minute_root)
    daily_path = Path(daily_root)
    samples = [_preflight_sample(minute_path, daily_path, sample) for sample in PREFLIGHT_SAMPLES]
    errors = [error for sample in samples for error in sample["sample_errors"]]
    result = {
        "contract": "TNM-1 fixed small-sample preflight",
        "source_read_only": True,
        "provider_calls": False,
        "production_db_writes": False,
        "l2_used": False,
        "ml_used": False,
        "st_filter_applied": False,
        "st_status": "unavailable_not_filtered",
        "corporate_action_filter": "unproven_not_applied",
        "capacity_verdict_5m": "capacity_unproven_no_frozen_rank",
        "ranking_status": "not_applicable_pre_discovery",
        "samples": samples,
        "preflight_status": "verified" if not errors else "partial",
        "execution_label": "tnm1_preflight_verified" if not errors else "tnm1_partial",
    }
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    report_path = output_path / "preflight.json"
    if report_path.exists():
        attempt = 2
        while (output_path / f"preflight-attempt-{attempt:02d}.json").exists():
            attempt += 1
        report_path = output_path / f"preflight-attempt-{attempt:02d}.json"
    report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    digest = hashlib.sha256(report_path.read_bytes()).hexdigest()
    manifest_path = output_path / ("sha256_manifest.json" if report_path.name == "preflight.json" else f"sha256_manifest-{report_path.stem}.json")
    manifest_path.write_text(
        json.dumps({report_path.name: digest}, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return result, report_path, digest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Tail Next-Morning V1 read-only research preflight.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    preflight = subparsers.add_parser("preflight", help="run only the frozen three-sample source preflight")
    preflight.add_argument("--minute-root", required=True)
    preflight.add_argument("--daily-root", required=True)
    preflight.add_argument("--output-dir", required=True)
    dev_preflight = subparsers.add_parser("dev-preflight", help="run only the fixed 2023 all-market development canary")
    dev_run = subparsers.add_parser("dev-run", help="run the frozen 2023-2024 development batch after TNM-2AR approval")
    for command in (dev_preflight, dev_run):
        command.add_argument("--minute-root", required=True)
        command.add_argument("--daily-root", required=True)
        command.add_argument("--output-dir", required=True)
        command.add_argument("--resume-run-id")
    args = parser.parse_args(argv)
    if args.command == "preflight":
        result, report_path, digest = run_preflight(args.minute_root, args.daily_root, args.output_dir)
        print(
            f"status={result['preflight_status']} label={result['execution_label']} "
            f"report={report_path} sha256={digest}"
        )
        return 0 if result["preflight_status"] == "verified" else 2
    if args.command in {"dev-preflight", "dev-run"}:
        canary = args.command == "dev-preflight"
        result, run_dir = run_development(
            args.minute_root,
            args.daily_root,
            args.output_dir,
            canary=canary,
            resume_run_id=args.resume_run_id,
        )
        print(
            f"status=verified label={'tnm2a_runner_verified' if canary else 'tnm2_development_completed'} "
            f"run_dir={run_dir} targets={len(result['target_dates'])}"
        )
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
