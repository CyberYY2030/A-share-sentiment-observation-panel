"""Fail-closed primitives for the Tail Next-Morning V1 research card.

This module deliberately stops at source validation and a fixed small preflight.
It does not select features, run a historical batch, write a database, or call a
provider.  Feature snapshots only receive information available at D 14:50;
outcomes are calculated by a separate function.
"""

from __future__ import annotations

import argparse
from bisect import bisect_left
import csv
from datetime import datetime, timedelta, timezone
import gzip
import hashlib
import io
import json
import math
import os
import socket
import subprocess
import tempfile
import zipfile
import xml.etree.ElementTree as ElementTree
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
TASK_CARD = Path(__file__).resolve().parents[1] / "docs" / "superpowers" / "specs" / "2026-08-24-tail-next-morning-v1-task-cards.md"


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
    # These two D+1 consumers are intentionally independent.  A malformed
    # 09:30--10:00 diagnostic bar must not turn an otherwise executable
    # 10:00--10:05 sale into a delayed exit.
    diagnostic_end = _window_index("10:00")
    diagnostic_reason = reason_for(0, diagnostic_end)
    if diagnostic_reason is None:
        potential = values[:diagnostic_end]
        result["morning_diagnostic"] = {
            "status": "ready",
            "mfe_high": float(potential[:, 1].max()),
            "mae_low": float(potential[:, 2].min()),
        }
    else:
        result["morning_diagnostic"] = {"status": "invalid", "reason": diagnostic_reason, "mfe_high": None, "mae_low": None}
    result["morning_sell"] = compact_vwap("10:00", "10:05")
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


def _xlsx_active_sheet_path(archive: zipfile.ZipFile) -> str:
    workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
    active_tab = int(next((node.attrib.get("activeTab", "0") for node in workbook.iter() if node.tag.endswith("}workbookView")), "0"))
    sheets = [node for node in workbook.iter() if node.tag.endswith("}sheet")]
    if active_tab >= len(sheets):
        raise ValueError("active_sheet_missing")
    relationship_id = next((value for key, value in sheets[active_tab].attrib.items() if key.endswith("}id")), None)
    relationships = ElementTree.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    target = next((node.attrib.get("Target") for node in relationships if node.attrib.get("Id") == relationship_id), None)
    if not target:
        raise ValueError("active_sheet_relationship_missing")
    clean = target.lstrip("/").replace("../", "")
    return clean if clean.startswith("xl/") else "xl/" + clean


def _xlsx_shared_strings(archive: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    result: list[str] = []
    with archive.open("xl/sharedStrings.xml") as payload:
        for _, node in ElementTree.iterparse(payload, events=("end",)):
            if node.tag.endswith("}si"):
                result.append("".join(child.text or "" for child in node.iter() if child.tag.endswith("}t")))
                node.clear()
    return result


def _xlsx_cell_value(cell: Any, shared_strings: list[str]) -> str:
    raw = next((node.text or "" for node in cell if node.tag.endswith("}v")), "")
    if cell.attrib.get("t") == "s":
        return shared_strings[int(raw)]
    if cell.attrib.get("t") == "inlineStr":
        return "".join(node.text or "" for node in cell.iter() if node.tag.endswith("}t"))
    return raw


def _xlsx_column(cell_reference: str) -> str:
    return "".join(character for character in cell_reference if character.isalpha())


def _xlsx_date_values(path: Path) -> tuple[str, ...]:
    """Read the active sheet's declared `date` column without loading K-line values."""
    with zipfile.ZipFile(path) as archive:
        shared_strings = _xlsx_shared_strings(archive)
        dates: set[str] = set()
        date_column: str | None = None
        with archive.open(_xlsx_active_sheet_path(archive)) as payload:
            for _, row in ElementTree.iterparse(payload, events=("end",)):
                if not row.tag.endswith("}row"):
                    continue
                cells = list(row)
                if row.attrib.get("r") == "1":
                    for cell in cells:
                        if _xlsx_cell_value(cell, shared_strings).strip().lower() == "date":
                            date_column = _xlsx_column(cell.attrib.get("r", ""))
                elif date_column:
                    for cell in cells:
                        if _xlsx_column(cell.attrib.get("r", "")) != date_column:
                            continue
                        raw = _xlsx_cell_value(cell, shared_strings).strip()
                        try:
                            numeric = float(raw)
                        except (TypeError, ValueError):
                            numeric = None
                        parsed = (
                            datetime(1899, 12, 30) + timedelta(days=numeric)
                            if numeric is not None and math.isfinite(numeric) and abs(numeric) < 1_000_000
                            else pd.to_datetime(raw, errors="coerce")
                        )
                        if not pd.isna(parsed):
                            dates.add(parsed.date().isoformat())
                        break
                row.clear()
    return tuple(sorted(dates))


def _listing_base(
    daily_root: str | Path,
    code: str,
    cache: dict[str, dict[str, Any]],
    *,
    allowed_daily_files: set[str] | None = None,
) -> dict[str, Any]:
    normalized = canonical_code(code)
    if normalized in cache:
        return cache[normalized]
    path = Path(daily_root) / f"{normalized}.xlsx"
    relative_path = path.relative_to(Path(daily_root)).as_posix() if path.exists() else None
    if allowed_daily_files is not None and relative_path is not None and relative_path not in allowed_daily_files:
        raise TailDataError("daily_k_read_outside_frozen_input")
    if not path.exists():
        value = {"status": "missing", "daily_columns": [], "listing_first_date": None}
    else:
        try:
            # The only K-line field consumed by TNM is `date`.  Reading the
            # active-sheet XML directly avoids materialising other indicators.
            dates = _xlsx_date_values(path)
            value = {
                "status": "ready" if dates else "unreadable",
                "daily_columns": ["date"],
                "listing_first_date": dates[0] if dates else None,
                "daily_dates": dates,
            }
        except Exception:
            value = {"status": "unreadable", "daily_columns": [], "listing_first_date": None, "daily_dates": ()}
    cache[normalized] = value
    return value


def development_listing_evidence(
    daily_root: str | Path,
    code: str,
    trade_date: str,
    minute_visible_sessions: int,
    cache: dict[str, dict[str, Any]],
    *,
    allowed_daily_files: set[str] | None = None,
) -> dict[str, Any]:
    """Use only daily-K date evidence; never read K-line outcomes or amount."""
    base = _listing_base(daily_root, code, cache, allowed_daily_files=allowed_daily_files)
    first_date = base.get("listing_first_date")
    if base.get("status") == "ready" and first_date is not None:
        history = bisect_left(base.get("daily_dates", ()), pd.Timestamp(str(trade_date)).date().isoformat())
        return {
            "listing_age_source": "daily_k_exact_date_sessions",
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
    if next_day is None:
        return {
            **_unavailable_outcome("delayed_exit_required", dict(buy)),
            "outcome_status": "delayed_exit_required",
            "exit_date": None,
        }
    diagnostic = next_day.get("morning_diagnostic", {})
    if diagnostic.get("status") == "ready":
        mfe = float(diagnostic["mfe_high"] / float(buy["vwap"]) - 1.0)
        mae = float(diagnostic["mae_low"] / float(buy["vwap"]) - 1.0)
    else:
        mfe = None
        mae = None
    sell = next_day.get("morning_sell", {})
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
    return _frozen_contract_hash(TASK_CARD)


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


def _zip_input_identity(source: Path) -> dict[str, Any]:
    """Record ZIP central-directory identity without consuming member payloads."""
    with zipfile.ZipFile(source) as archive:
        central_directory = [
            {
                "relative_path": info.filename,
                "uncompressed_size": int(info.file_size),
                "crc": f"{int(info.CRC):08x}",
            }
            for info in sorted(archive.infolist(), key=lambda item: item.filename)
        ]
    stat = source.stat()
    return {
        "path": str(source),
        "size_bytes": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
        "member_count": len(central_directory),
        "central_directory_digest": _json_digest(central_directory),
    }


def _directory_input_identity(source: Path) -> dict[str, Any]:
    candidates = []
    for item in sorted(source.rglob("*.csv")):
        stat = item.stat()
        candidates.append({"relative_path": item.relative_to(source).as_posix(), "size_bytes": int(stat.st_size), "mtime_ns": int(stat.st_mtime_ns)})
    return {
        "path": str(source),
        "candidate_count": len(candidates),
        "total_bytes": sum(item["size_bytes"] for item in candidates),
        "tree_digest": _json_digest(candidates),
    }


def _daily_k_input_identity(daily_root: str | Path) -> dict[str, Any]:
    root = Path(daily_root)
    candidates = []
    for item in sorted(root.rglob("*.xlsx")):
        stat = item.stat()
        candidates.append({"relative_path": item.relative_to(root).as_posix(), "size_bytes": int(stat.st_size), "mtime_ns": int(stat.st_mtime_ns)})
    return {
        "root": str(root),
        "candidate_count": len(candidates),
        "total_bytes": sum(item["size_bytes"] for item in candidates),
        "tree_digest": _json_digest(candidates),
        "files": candidates,
    }


def _input_manifest(
    minute_root: str | Path,
    daily_root: str | Path,
    trade_dates: Iterable[str],
    allowed_years: Iterable[str],
) -> dict[str, Any]:
    allowed = set(str(year) for year in allowed_years)
    records: list[dict[str, Any]] = []
    for trade_date in sorted(set(trade_dates)):
        if str(trade_date)[:4] not in allowed:
            raise TailDataError(f"development_year_guard:{trade_date}")
        source, source_kind = locate_day_source(minute_root, trade_date)
        identity = _zip_input_identity(source) if source_kind == "zip" else _directory_input_identity(source)
        records.append({"trade_date": str(trade_date), "source_kind": source_kind, **identity})
    return {"minute_containers": records, "daily_k_root": _daily_k_input_identity(daily_root)}


def _prepare_development_run(
    output_dir: str | Path,
    *,
    mode: str,
    input_manifest: Mapping[str, Any],
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


def _pid_is_alive(pid: Any) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _acquire_run_lock(run_dir: Path, run_manifest: Mapping[str, Any], *, explicit_resume: bool) -> Path:
    lock = run_dir / "run.lock"
    owner = {
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "run_id": run_manifest["run_id"],
        "run_hash": run_manifest["run_hash"],
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        try:
            existing = json.loads(lock.read_text(encoding="utf-8"))
        except Exception as parse_exc:
            raise TailDataError("single_writer_lock_exists") from parse_exc
        if not explicit_resume:
            raise TailDataError("single_writer_lock_exists") from exc
        if existing.get("host") != socket.gethostname() or existing.get("run_hash") != run_manifest["run_hash"]:
            raise TailDataError("resume_lock_owner_mismatch") from exc
        if _pid_is_alive(existing.get("pid")):
            raise TailDataError("resume_lock_pid_alive") from exc
        evidence_dir = run_dir / "lock_evidence"
        evidence_name = f"superseded-{_json_digest(existing)[:16]}.json"
        _atomic_write_json(evidence_dir / evidence_name, existing)
        lock.unlink()
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(_canonical_json_bytes(owner).decode("utf-8"))
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


def _state_path(run_dir: Path) -> Path:
    return run_dir / "resume_state.json"


def _read_resume_state(run_dir: Path, run_hash: str) -> dict[str, Any] | None:
    path = _state_path(run_dir)
    if not path.exists():
        return None
    state = json.loads(path.read_text(encoding="utf-8"))
    if state.get("run_hash") != run_hash:
        raise TailDataError("resume_state_hash_mismatch")
    return state


def _checkpointable_statistics(statistics: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Persist only rolling feature/outcome windows; delayed grids are one-day consumers."""
    return {
        code: {key: value for key, value in stat.items() if key != "delayed_exit_windows"}
        for code, stat in statistics.items()
    }


def _write_resume_state(
    run_dir: Path,
    *,
    run_hash: str,
    next_index: int,
    last_trade_date: str | None,
    source_by_date: Mapping[str, Mapping[str, Mapping[str, Any]]],
    source_records: Mapping[str, Mapping[str, Any]],
    visible_history: Mapping[str, int],
    pending: Mapping[str, Mapping[str, Any]],
    updates: Mapping[str, Mapping[str, Any]],
    completed_targets: Iterable[str],
) -> None:
    _atomic_write_json(
        _state_path(run_dir),
        {
            "run_hash": run_hash,
            "source_frontier": {"next_index": int(next_index), "last_trade_date": last_trade_date},
            "rolling_history": {date: _checkpointable_statistics(stats) for date, stats in source_by_date.items()},
            "source_records": dict(source_records),
            "visible_history": {code: int(value) for code, value in visible_history.items()},
            "pending_exits": dict(pending),
            "outcome_updates": dict(updates),
            "completed_targets": sorted(set(completed_targets)),
        },
    )


def _verify_completed_artifacts(run_dir: Path, completion: Mapping[str, Any], run_hash: str) -> dict[str, Any] | None:
    if completion.get("status") != "SUCCEEDED" or completion.get("run_hash") != run_hash:
        return None
    artifact_hashes = completion.get("economic_artifacts")
    if not isinstance(artifact_hashes, Mapping):
        raise TailDataError("completion_artifact_manifest_missing")
    manifest_path = run_dir / "sha256_manifest.json"
    if not manifest_path.exists() or json.loads(manifest_path.read_text(encoding="utf-8")) != artifact_hashes:
        raise TailDataError("completion_artifact_manifest_mismatch")
    for name, expected in artifact_hashes.items():
        path = run_dir / str(name)
        if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise TailDataError("completion_artifact_hash_mismatch")
    result_path = run_dir / "development_results.json"
    if not result_path.exists():
        raise TailDataError("completion_results_missing")
    return json.loads(result_path.read_text(encoding="utf-8"))


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
    *,
    allowed_daily_files: set[str] | None = None,
    on_listing_progress: Any = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    ordered_days = sorted(day_statistics.items())
    for position, (code, day) in enumerate(ordered_days, start=1):
        if on_listing_progress is not None and (position == 1 or position % 100 == 0 or position == len(ordered_days)):
            on_listing_progress(position, len(ordered_days))
        listing = development_listing_evidence(
            daily_root,
            code,
            target_date,
            int(visible_history.get(code, 0)),
            listing_cache,
            allowed_daily_files=allowed_daily_files,
        )
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


def _daily_deciles_and_rank_ic(daily_rows: Mapping[str, list[dict[str, Any]]]) -> tuple[dict[str, Any], dict[str, Any]]:
    deciles: dict[str, Any] = {}
    rank_ic: dict[str, Any] = {}
    for trade_date, rows in sorted(daily_rows.items()):
        ready = [row for row in rows if row.get("feature_status") == "ready"]
        returns = [_net30(row.get("outcome")) for row in ready]
        deciles[trade_date] = {}
        rank_ic[trade_date] = {}
        for feature in FEATURE_NAMES:
            buckets: list[list[float]] = [[] for _ in range(10)]
            pairs: list[tuple[float, float]] = []
            for row, net30 in zip(ready, returns):
                percentile = row.get("feature_percentiles", {}).get(feature)
                if isinstance(percentile, (int, float)) and math.isfinite(float(percentile)):
                    bucket = min(9, max(0, int(float(percentile) * 10)))
                    if net30 is not None:
                        buckets[bucket].append(float(net30))
                        pairs.append((float(percentile), float(net30)))
            deciles[trade_date][feature] = [
                {"decile": index + 1, "count": len(values), "mean_net30": float(sum(values) / len(values)) if values else None}
                for index, values in enumerate(buckets)
            ]
            if len(pairs) < 2 or len(pairs) != len(ready):
                rank_ic[trade_date][feature] = {"status": "unavailable", "spearman_rank_ic": None, "count": len(pairs)}
            else:
                coefficient = _spearman_rank_correlation([pair[0] for pair in pairs], [pair[1] for pair in pairs])
                rank_ic[trade_date][feature] = {
                    "status": "ready" if pd.notna(coefficient) else "unavailable",
                    "spearman_rank_ic": float(coefficient) if pd.notna(coefficient) else None,
                    "count": len(pairs),
                }
    return deciles, rank_ic


def _spearman_rank_correlation(left: list[float], right: list[float]) -> float | None:
    """Tie-aware Spearman correlation without adding scipy to the frozen runner."""
    if len(left) != len(right) or len(left) < 2:
        return None

    def ranks(values: list[float]) -> list[float]:
        ranked = sorted(enumerate(values), key=lambda item: item[1])
        result = [0.0] * len(values)
        index = 0
        while index < len(ranked):
            end = index + 1
            while end < len(ranked) and ranked[end][1] == ranked[index][1]:
                end += 1
            average = (index + 1 + end) / 2.0
            for original, _ in ranked[index:end]:
                result[original] = average
            index = end
        return result

    left_ranks, right_ranks = ranks(left), ranks(right)
    left_mean, right_mean = sum(left_ranks) / len(left_ranks), sum(right_ranks) / len(right_ranks)
    numerator = sum((a - left_mean) * (b - right_mean) for a, b in zip(left_ranks, right_ranks))
    denominator = math.sqrt(sum((a - left_mean) ** 2 for a in left_ranks) * sum((b - right_mean) ** 2 for b in right_ranks))
    return None if denominator == 0 else float(numerator / denominator)


def _strategy_performance(portfolios: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    by_year: dict[str, list[float]] = {year: [] for year in DEVELOPMENT_YEARS}
    daily: list[dict[str, Any]] = []
    equity = 1.0
    peak = 1.0
    max_drawdown = 0.0
    blocked = False
    for portfolio in sorted(portfolios, key=lambda item: str(item["trade_date"])):
        net30 = portfolio["strategy"].get("net30")
        trade_date = str(portfolio["trade_date"])
        if not isinstance(net30, (int, float)) or not math.isfinite(float(net30)):
            blocked = True
            daily.append({"trade_date": trade_date, "net30": None, "equity": None})
            continue
        value = float(net30)
        by_year.setdefault(trade_date[:4], []).append(value)
        equity *= 1.0 + value
        peak = max(peak, equity)
        max_drawdown = min(max_drawdown, equity / peak - 1.0)
        daily.append({"trade_date": trade_date, "net30": value, "equity": equity})
    annual = {
        year: {"available_days": len(values), "mean_daily_net30": float(sum(values) / len(values)) if values else None}
        for year, values in by_year.items()
    }
    return {"annual": annual, "compound_equity_curve": daily, "max_drawdown": max_drawdown if daily and not blocked else None, "blocked_unresolved_exit": blocked}


def _execution_summary(daily_rows: Mapping[str, list[dict[str, Any]]]) -> dict[str, Any]:
    counters = {"feature_isolated": 0, "cash_unfilled_buy": 0, "delayed_exit": 0, "unresolved_exit": 0, "outcome_unavailable": 0}
    for rows in daily_rows.values():
        for row in rows:
            if row.get("feature_status") != "ready":
                counters["feature_isolated"] += 1
                continue
            status = (row.get("outcome") or {}).get("outcome_status")
            if status == "cash_unfilled_buy":
                counters["cash_unfilled_buy"] += 1
            elif status == "delayed_exit_required":
                counters["delayed_exit"] += 1
            elif status == "unresolved_exit_at_phase_end":
                counters["unresolved_exit"] += 1
            elif status != "ready":
                counters["outcome_unavailable"] += 1
    return counters


def _frozen_execution_contract() -> dict[str, Any]:
    return {
        "signal_window": "D [14:20,14:50)",
        "buy_window": "D [14:51,14:56)",
        "opening_sell_window": "D+1 [10:00,10:05)",
        "eligibility": {"d_minus_1_amount": ">500000000", "minimum_listing_history_sessions": MIN_HISTORY_SESSIONS},
        "costs": {"gross": 0.0, "net15bps": 0.0015, "net30bps": 0.0030},
        "slots": SLOT_COUNT,
        "tie_break": "score_desc_then_sec_code_asc",
        "baselines": ["eligible_pool_equal_weight", "sha256_seeded_random_top10", "tail_return_relative_top10"],
        "delayed_exit": {"grid_minutes": 5, "first_day": list(DELAYED_FIRST_DAY_WINDOWS), "later_days": list(DELAYED_LATER_DAY_WINDOWS), "no_year_crossing": True, "phase_end": "unresolved_exit_at_phase_end"},
    }


def _finalize_economic_outputs(
    run_dir: Path,
    *,
    mode: str,
    canary: bool,
    target_dates: list[str],
    daily_rows: Mapping[str, list[dict[str, Any]]],
    run_manifest: Mapping[str, Any] | None = None,
    approved_commit: str | None = None,
) -> tuple[dict[str, Any], dict[str, str]]:
    selection = select_development_features(daily_rows)
    if canary:
        directions = {"tail_return_rel": 1}
        selection_status = "canary_probe_not_frozen"
    else:
        directions = {row["feature"]: int(row["direction"]) for row in selection["selected_features"]}
        selection_status = "provisional"
    portfolios = [_portfolio_day(daily_rows[date], date, directions) for date in target_dates] if directions else []
    capacity = _capacity_sleeve_ledger({date: daily_rows[date] for date in target_dates}, directions) if directions else {
        "capacity_verdict_5m": "capacity_unproven",
        "reason": "no_selected_features",
        "total_capital_yuan": CAPACITY_TOTAL_YUAN,
        "per_stock_yuan": CAPACITY_PER_STOCK_YUAN,
        "sleeve_count": SLOT_COUNT,
    }
    deciles, rank_ic = _daily_deciles_and_rank_ic(daily_rows)
    performance = _strategy_performance(portfolios)
    annual_ok = all(
        isinstance(performance["annual"].get(year, {}).get("mean_daily_net30"), (int, float))
        and float(performance["annual"][year]["mean_daily_net30"]) > 0
        for year in DEVELOPMENT_YEARS
    )
    no_unresolved = not performance["blocked_unresolved_exit"] and not any(portfolio["strategy"].get("blocked") for portfolio in portfolios)
    if not canary:
        selection_status = "ready" if directions and selection["status"] == "ready" and annual_ok and no_unresolved else "no_stable_development_signal"
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
        "daily_deciles": deciles,
        "daily_spearman_rank_ic": rank_ic,
        "portfolios": portfolios,
        "strategy_performance": performance,
        "execution_summary": _execution_summary(daily_rows),
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
    frozen_path = run_dir / "frozen_rule.json"
    if not canary and selection_status == "ready":
        frozen_rule = {
            "selected_features": selection["selected_features"],
            "selection_status": selection_status,
            "corporate_action_filter": "unproven_not_applied",
            "st_filter_applied": False,
            "execution_contract": _frozen_execution_contract(),
            "binding": {
                "spec_hash": (run_manifest or {}).get("spec_hash"),
                "approved_commit": approved_commit,
                "runner_source_hash": (run_manifest or {}).get("runner_source_hash"),
                "input_manifest_hash": (run_manifest or {}).get("input_manifest_hash"),
                "run_id": (run_manifest or {}).get("run_id"),
                "run_hash": (run_manifest or {}).get("run_hash"),
                "economic_artifact_hashes": artifact_hashes,
            },
        }
        _atomic_write_json(frozen_path, frozen_rule)
        artifact_hashes[frozen_path.name] = hashlib.sha256(frozen_path.read_bytes()).hexdigest()
    elif frozen_path.exists():
        # A resumed development run that fails its stability gate must never
        # retain a prior usable rule in the same identity directory.
        frozen_path.unlink()
    _atomic_write_json(run_dir / "sha256_manifest.json", artifact_hashes)
    return economic, artifact_hashes


def _validate_approved_development_commit(approved_commit: str | None) -> str:
    if not approved_commit:
        raise TailDataError("dev_run_requires_approved_commit")
    try:
        resolved = subprocess.run(
            ["git", "rev-parse", str(approved_commit)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        ).stdout.strip()
    except Exception as exc:
        raise TailDataError("approved_commit_unresolvable") from exc
    if _current_git_commit() != resolved:
        raise TailDataError("approved_commit_not_current_head")
    owned = (str(TASK_CARD), str(Path(__file__)), "tests/test_tail_next_morning.py")
    check = subprocess.run(["git", "diff", "--quiet", "HEAD", "--", *owned], check=False)
    cached = subprocess.run(["git", "diff", "--cached", "--quiet", "HEAD", "--", *owned], check=False)
    if check.returncode or cached.returncode:
        raise TailDataError("dev_run_task_owned_paths_not_clean")
    return resolved


def run_development(
    minute_root: str | Path,
    daily_root: str | Path,
    output_dir: str | Path,
    *,
    canary: bool,
    resume_run_id: str | None = None,
    approved_commit: str | None = None,
) -> tuple[dict[str, Any], Path]:
    """One read-only production path for the fixed canary and later dev-run.

    `canary=True` is TNM-2A's sole executable path: exactly the first ten 2023
    targets and no frozen rule.  The `False` branch exists for TNM-2B after its
    independent approval; this task never invokes it.
    """
    approved = None if canary else _validate_approved_development_commit(approved_commit)
    allowed_years = ("2023",) if canary else DEVELOPMENT_YEARS
    calendar = _discover_trade_dates(minute_root, allowed_years)
    target_dates, process_dates = _target_dates_for_development(calendar, canary=canary)
    manifest = _input_manifest(minute_root, daily_root, process_dates, allowed_years)
    mode = "dev-preflight" if canary else "dev-run"
    run_dir, run_manifest = _prepare_development_run(output_dir, mode=mode, input_manifest=manifest, resume_run_id=resume_run_id)
    completion_path = run_dir / "completion.json"
    completed = json.loads(completion_path.read_text(encoding="utf-8")) if completion_path.exists() else None
    completed_result = _verify_completed_artifacts(run_dir, completed, run_manifest["run_hash"]) if completed else None
    if completed_result is not None:
        return completed_result, run_dir
    if completed and not resume_run_id:
        raise TailDataError("resume_required_for_terminal_run")
    lock = _acquire_run_lock(run_dir, run_manifest, explicit_resume=bool(resume_run_id))
    try:
        state = _read_resume_state(run_dir, run_manifest["run_hash"]) if resume_run_id else None
        source_by_date: dict[str, dict[str, dict[str, Any]]] = dict((state or {}).get("rolling_history", {}))
        source_records: dict[str, dict[str, Any]] = dict((state or {}).get("source_records", {}))
        visible_history: dict[str, int] = {code: int(value) for code, value in (state or {}).get("visible_history", {}).items()}
        listing_cache: dict[str, dict[str, Any]] = {}
        pending: dict[str, dict[str, Any]] = dict((state or {}).get("pending_exits", {}))
        updates_path = run_dir / "outcome_updates.json"
        updates: dict[str, dict[str, Any]] = dict((state or {}).get("outcome_updates", {}))
        if not updates and updates_path.exists():
            updates = json.loads(updates_path.read_text(encoding="utf-8"))
        completed_target_dates: set[str] = set((state or {}).get("completed_targets", ()))
        next_index = int((state or {}).get("source_frontier", {}).get("next_index", 0))
        if next_index < 0 or next_index > len(process_dates):
            raise TailDataError("resume_source_frontier_invalid")
        _atomic_write_json(
            run_dir / "progress.json",
            {
                "run_id": run_manifest["run_id"],
                "status": "RUNNING",
                "completed_target_dates": len(completed_target_dates),
                "total_target_dates": len(target_dates),
                "source_frontier": next_index,
            },
        )
        allowed_daily_files = {str(item["relative_path"]) for item in manifest["daily_k_root"]["files"]}
        for index in range(next_index, len(process_dates)):
            trade_date = process_dates[index]
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
                if target_date in target_dates and target_date not in completed_target_dates:
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
                            allowed_daily_files=allowed_daily_files,
                            on_listing_progress=lambda completed, total: _atomic_write_json(
                                run_dir / "progress.json",
                                {
                                    "run_id": run_manifest["run_id"],
                                    "status": "RUNNING",
                                    "completed_target_dates": len(completed_target_dates),
                                    "total_target_dates": len(target_dates),
                                    "source_frontier": index,
                                    "listing_target_date": target_date,
                                    "listing_codes_completed": completed,
                                    "listing_codes_total": total,
                                },
                            ),
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
                    completed_target_dates.add(target_date)
                    _atomic_write_json(
                        run_dir / "progress.json",
                        {
                            "run_id": run_manifest["run_id"],
                            "status": "RUNNING",
                            "completed_target_dates": len(completed_target_dates),
                            "total_target_dates": len(target_dates),
                            "source_frontier": index + 1,
                        },
                    )
            for code in statistics:
                visible_history[code] = int(visible_history.get(code, 0)) + 1
            _atomic_write_json(updates_path, updates)
            if index >= 4:
                source_by_date.pop(process_dates[index - 4], None)
                source_records.pop(process_dates[index - 4], None)
            _write_resume_state(
                run_dir,
                run_hash=run_manifest["run_hash"],
                next_index=index + 1,
                last_trade_date=trade_date,
                source_by_date=source_by_date,
                source_records=source_records,
                visible_history=visible_history,
                pending=pending,
                updates=updates,
                completed_targets=completed_target_dates,
            )
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
        economic, hashes = _finalize_economic_outputs(
            run_dir,
            mode=mode,
            canary=canary,
            target_dates=target_dates,
            daily_rows=daily_rows,
            run_manifest=run_manifest,
            approved_commit=approved,
        )
        completion = {"status": "SUCCEEDED", "run_id": run_manifest["run_id"], "run_hash": run_manifest["run_hash"], "economic_artifacts": hashes}
        _atomic_write_json(completion_path, completion)
        _atomic_write_json(run_dir / "progress.json", {"run_id": run_manifest["run_id"], "status": "SUCCEEDED", "completed_target_dates": len(target_dates), "total_target_dates": len(target_dates)})
        return economic, run_dir
    except KeyboardInterrupt:
        _atomic_write_json(completion_path, {"status": "CANCELLED", "run_id": run_manifest["run_id"], "run_hash": run_manifest["run_hash"], "reason": "keyboard_interrupt"})
        raise
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
    dev_run.add_argument("--approved-commit", required=True)
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
            approved_commit=None if canary else args.approved_commit,
        )
        print(
            f"status=verified label={'tnm2a_runner_verified' if canary else 'tnm2_development_completed'} "
            f"run_dir={run_dir} targets={len(result['target_dates'])}"
        )
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
