"""TNM V2 fixed-rule canary: causal trend-continuation gates and rankings.

This module deliberately reuses V1's source location, parsing and window
validation primitives.  Its only reader is a canary-specific, one-container
enumerator which immediately reduces each CSV to the additional V2 statistics
that V1 did not consume.  It never changes V1, reads only 2024 dependencies
for the five frozen target dates, and has no development-batch command.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import date, timedelta
import csv
import gzip
import hashlib
import io
import json
import math
from pathlib import Path, PurePosixPath
import subprocess
from typing import Any, Iterable, Mapping
import zipfile

import pandas as pd

from mining.tail_next_morning import (
    MIN_HISTORY_SESSIONS,
    TailDataError,
    canonical_code,
    development_listing_evidence,
    is_a_share_code,
    load_minute_session,
    locate_day_source,
    parse_minute_frame,
    validate_minute_window,
    vwap_for_window,
    window_bars,
)


TASK_CARD = Path(__file__).resolve().parents[1] / "docs" / "superpowers" / "specs" / "2026-08-25-tail-next-morning-v2-task-cards.md"
TARGET_DATES = ("20240813", "20240826", "20240827", "20240923", "20240926")
MIN_D1_AMOUNT = 200_000_000.0
A_LIMIT = 3
B_LIMIT = 2
FIXTURES = {
    ("20240923", "300085"): {"a_shape_pass": True, "liquidity_pass": True, "a_top": True},
    ("20240926", "300339"): {"a_shape_pass": True, "liquidity_pass": True, "a_top": True},
    ("20240826", "300972"): {"b_shape_pass": True, "liquidity_pass": False, "not_selected": True},
    ("20240827", "300972"): {"b_shape_pass": False, "reason_contains": "position1450<0.55", "liquidity_pass": False},
    ("20240813", "300328"): {"b_shape_pass": True, "liquidity_pass": True},
}


def _finite(value: object) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(float(value))


def _reason(prefix: str, value: object) -> str:
    return f"{prefix}:{value}" if value is not None else prefix


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def _write_json(path: Path, value: Any) -> None:
    path.write_bytes(_json_bytes(value))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _spec_hash() -> str:
    text = TASK_CARD.read_text(encoding="utf-8").replace("\r\n", "\n")
    definition, marker, _ = text.partition("## 11. 执行结果")
    if not marker:
        raise TailDataError("v2_task_card_execution_marker_missing")
    return hashlib.sha256(definition.encode("utf-8")).hexdigest()


def _git_blob(path: Path) -> str:
    completed = subprocess.run(
        ["git", "hash-object", str(path)],
        cwd=Path(__file__).resolve().parents[1],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _code_identity() -> dict[str, str]:
    module = Path(__file__).resolve()
    test = module.parents[1] / "tests" / "test_tail_next_morning_v2.py"
    return {"module_blob": _git_blob(module), "test_blob": _git_blob(test)}


def _code_hash(identity: Mapping[str, str]) -> str:
    return hashlib.sha256(_json_bytes(dict(identity))).hexdigest()


def _day_key(value: date) -> str:
    return value.strftime("%Y%m%d")


def _calendar_for_targets(minute_root: str | Path, targets: Iterable[str]) -> tuple[list[str], dict[str, int]]:
    frozen = tuple(str(value) for value in targets)
    if frozen != TARGET_DATES:
        raise TailDataError("canary_target_dates_must_match_frozen_set")
    first = date(int(frozen[0][:4]), int(frozen[0][4:6]), int(frozen[0][6:])) - timedelta(days=35)
    last = date(int(frozen[-1][:4]), int(frozen[-1][4:6]), int(frozen[-1][6:])) + timedelta(days=7)
    sessions: list[str] = []
    cursor = first
    while cursor <= last:
        if cursor.weekday() < 5:
            key = _day_key(cursor)
            try:
                locate_day_source(minute_root, key)
                sessions.append(key)
            except TailDataError as exc:
                if exc.reason != f"minute_day_missing:{key}":
                    raise
        cursor += timedelta(days=1)
    positions = {day: index for index, day in enumerate(sessions)}
    for target in frozen:
        index = positions.get(target)
        if index is None or index < 10 or index + 1 >= len(sessions):
            raise TailDataError(f"canary_calendar_missing_dependency:{target}")
    return sessions, positions


def _v2_statistics_from_frame(raw: pd.DataFrame) -> dict[str, Any]:
    """Add only V2's D-before-14:50 sufficient statistics using V1 guards."""
    try:
        bars = parse_minute_frame(raw)
    except TailDataError as exc:
        return {"status": "invalid", "reason": exc.reason}
    result: dict[str, Any] = {"status": "ready"}
    try:
        full = validate_minute_window(bars, "09:30", "15:00")
        result["history"] = {
            "status": "ready",
            "close": float(full.iloc[-1]["close"]),
            "high": float(full["high"].max()),
            "low": float(full["low"].min()),
            "full_amount": float(full["amount"].sum()),
        }
    except TailDataError as exc:
        result["history"] = {"status": "invalid", "reason": exc.reason}
    try:
        signal = validate_minute_window(bars, "09:30", "14:50")
        tail = window_bars(bars, "14:20", "14:50")
        amount = float(signal["amount"].sum())
        volume = float(signal["volume"].sum())
        high = float(signal["high"].max())
        low = float(signal["low"].min())
        result["signal"] = {
            "status": "ready",
            "open": float(signal.iloc[0]["open"]),
            "close": float(signal.iloc[-1]["close"]),
            "high": high,
            "low": low,
            "amount": amount,
            "volume": volume,
            "range1450": float(high / low - 1.0),
            "tail_return": float(tail.iloc[-1]["close"] / tail.iloc[0]["open"] - 1.0),
            "tail_location": float((tail.iloc[-1]["close"] - tail["low"].min()) / (tail["high"].max() - tail["low"].min()))
            if float(tail["high"].max() - tail["low"].min()) > 0
            else None,
        }
    except TailDataError as exc:
        result["signal"] = {"status": "invalid", "reason": exc.reason}
    try:
        validate_minute_window(bars, "14:51", "14:56")
        result["buy"] = vwap_for_window(bars, "14:51", "14:56")
    except TailDataError as exc:
        result["buy"] = {"status": "invalid_window", "reason": exc.reason, "vwap": None, "amount": None, "volume": None}
    return result


def _stats_from_payload(payload: bytes) -> dict[str, Any]:
    try:
        return _v2_statistics_from_frame(pd.read_csv(io.BytesIO(payload)))
    except Exception:
        return {"status": "invalid", "reason": "minute_csv_unreadable"}


def load_day_v2_statistics(minute_root: str | Path, trade_date: str) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Enumerate one day once and reuse V1's location/parser/causal validation."""
    key = str(trade_date).replace("-", "")
    if key[:4] != "2024":
        raise TailDataError(f"v2_canary_year_guard:{key}")
    source, source_kind = locate_day_source(minute_root, key)
    result: dict[str, dict[str, Any]] = {}
    duplicates: set[str] = set()
    if source_kind == "zip":
        with zipfile.ZipFile(source) as archive:
            members: dict[str, str] = {}
            for name in sorted(archive.namelist()):
                if name.endswith("/") or PurePosixPath(name).suffix.lower() != ".csv":
                    continue
                code = canonical_code(PurePosixPath(name).stem)
                if not is_a_share_code(code):
                    continue
                if code in members:
                    duplicates.add(code)
                else:
                    members[code] = name
            for code, name in members.items():
                result[code] = {"status": "invalid", "reason": "ambiguous_minute_code_file"} if code in duplicates else _stats_from_payload(archive.read(name))
    else:
        members: dict[str, Path] = {}
        for item in sorted(source.rglob("*.csv")):
            code = canonical_code(item.stem)
            if not is_a_share_code(code):
                continue
            if code in members:
                duplicates.add(code)
            else:
                members[code] = item
        for code, item in members.items():
            result[code] = {"status": "invalid", "reason": "ambiguous_minute_code_file"} if code in duplicates else _stats_from_payload(item.read_bytes())
    stat = source.stat()
    return result, {
        "trade_date": key,
        "source_kind": source_kind,
        "size_bytes": stat.st_size if source.is_file() else None,
        "mtime_ns": stat.st_mtime_ns,
        "container_open_count": 1,
        "universe_count": len(result),
        "invalid_file_count": sum(value.get("status") != "ready" for value in result.values()),
    }


def _history_ready(stat: Mapping[str, Any] | None) -> bool:
    return bool(stat and stat.get("status") == "ready" and stat.get("history", {}).get("status") == "ready")


def _signal_ready(stat: Mapping[str, Any] | None) -> bool:
    return bool(stat and stat.get("status") == "ready" and stat.get("signal", {}).get("status") == "ready")


def _base_row(code: str, day: Mapping[str, Any] | None, prior: list[Mapping[str, Any] | None]) -> dict[str, Any]:
    row: dict[str, Any] = {
        "sec_code": canonical_code(code),
        "common_quality_pass": False,
        "shape_pass": False,
        "a_shape_pass": False,
        "b_shape_pass": False,
        "liquidity_pass": False,
        "listing_pass": False,
        "eligible_pass": False,
        "a_failure_reasons": [],
        "b_failure_reasons": [],
        "failure_reasons": [],
        "st_filter_applied": False,
        "st_status": "unavailable_not_filtered",
        "corporate_action_filter": "unproven_not_applied",
    }
    if not is_a_share_code(code):
        row["failure_reasons"].append("non_a_share_prefix")
        return row
    if not _signal_ready(day):
        row["failure_reasons"].append(_reason("signal_invalid", (day or {}).get("signal", {}).get("reason")))
        return row
    if len(prior) != 10 or not all(_history_ready(value) and _signal_ready(value) for value in prior):
        row["failure_reasons"].append("missing_or_invalid_history_d10_to_d1")
        return row
    signal = day["signal"]
    previous = [value for value in prior if value is not None]
    prev = previous[-1]["history"]
    previous_signals = [value["signal"] for value in previous]
    close1450 = float(signal["close"])
    high1450 = float(signal["high"])
    low1450 = float(signal["low"])
    amount1450 = float(signal["amount"])
    volume1450 = float(signal["volume"])
    prev_close = float(prev["close"])
    if volume1450 <= 0 or amount1450 <= 0 or high1450 <= low1450 or prev_close <= 0:
        row["failure_reasons"].append("non_positive_common_denominator")
        return row
    prev_ma10 = float(pd.Series([value["history"]["close"] for value in previous]).mean())
    range_median = float(pd.Series([value["signal"]["range1450"] for value in previous]).median())
    activity_denominator = float(pd.Series([value["signal"]["amount"] for value in previous[-3:]]).median())
    pullback_denominator = float(max(value["signal"]["amount"] for value in previous[-5:]))
    prior10_high = float(max(value["history"]["high"] for value in previous))
    if min(prev_ma10, range_median, activity_denominator, pullback_denominator, prior10_high) <= 0:
        row["failure_reasons"].append("non_positive_history_denominator")
        return row
    metrics = {
        "ret1450": close1450 / prev_close - 1.0,
        "position1450": (close1450 - low1450) / (high1450 - low1450),
        "vwap1450": amount1450 / volume1450,
        "amount1450": amount1450,
        "prev_close": prev_close,
        "prev_ma10": prev_ma10,
        "range_expansion": float(signal["range1450"]) / range_median,
        "activity": amount1450 / activity_denominator,
        "tail_return": signal.get("tail_return"),
        "tail_location": signal.get("tail_location"),
        "prior10_high": prior10_high,
        "prior10_runup": prior10_high / float(previous[0]["history"]["close"]) - 1.0,
        "drawdown": close1450 / prior10_high - 1.0,
        "pullback_volume_ratio": amount1450 / pullback_denominator,
        "body1450": close1450 / float(signal["open"]) - 1.0,
        "open_D": float(signal["open"]),
        "d1_amount": float(prev["full_amount"]),
        "close1450": close1450,
    }
    metrics["vwap_dist"] = close1450 / metrics["vwap1450"] - 1.0
    row.update(metrics)
    row["common_quality_pass"] = True
    row["liquidity_pass"] = metrics["d1_amount"] > MIN_D1_AMOUNT
    if not row["liquidity_pass"]:
        row["failure_reasons"].append("d1_amount_not_strictly_above_200m")
    return row


def _a_shape(row: dict[str, Any]) -> None:
    if not row["common_quality_pass"]:
        row["a_failure_reasons"].append("common_quality_failed")
        return
    gates = (
        (row["ret1450"] >= 0.04, "ret1450<0.04"),
        (row["position1450"] >= 0.55, "position1450<0.55"),
        (row["vwap_dist"] >= 0.01, "vwap_dist<0.01"),
        (row["activity"] >= 1.50, "activity<1.50"),
        (row["close1450"] > row["prev_ma10"], "close1450<=prev_ma10"),
    )
    row["a_failure_reasons"] = [reason for passed, reason in gates if not passed]
    row["a_shape_pass"] = not row["a_failure_reasons"]


def _b_shape(row: dict[str, Any]) -> None:
    if not row["common_quality_pass"]:
        row["b_failure_reasons"].append("common_quality_failed")
        return
    gates = (
        (row["prior10_runup"] >= 0.15, "prior10_runup<0.15"),
        (-0.15 <= row["drawdown"] <= -0.03, "drawdown_outside_-0.15_to_-0.03"),
        (row["close1450"] > row["prev_ma10"], "close1450<=prev_ma10"),
        (row["pullback_volume_ratio"] <= 0.80, "pullback_volume_ratio>0.80"),
        (row["close1450"] > max(row["prev_close"], row["open_D"]), "close1450<=max(open_D,prev_close)"),
        (row["position1450"] >= 0.55, "position1450<0.55"),
    )
    row["b_failure_reasons"] = [reason for passed, reason in gates if not passed]
    row["b_shape_pass"] = not row["b_failure_reasons"]


def build_signal_row(code: str, day: Mapping[str, Any] | None, prior: Iterable[Mapping[str, Any] | None], listing: Mapping[str, Any]) -> dict[str, Any]:
    """Pure V2 gates.  It never receives a buy window or D+1 information."""
    row = _base_row(code, day, list(prior))
    _a_shape(row)
    _b_shape(row)
    row["shape_pass"] = bool(row["a_shape_pass"] or row["b_shape_pass"])
    row["listing_pass"] = listing.get("listing_history_count_status") == "at_least_threshold" or int(listing.get("listing_history_sessions") or 0) >= MIN_HISTORY_SESSIONS
    row["listing_age_source"] = listing.get("listing_age_source")
    row["listing_history_sessions"] = listing.get("listing_history_sessions")
    row["listing_history_count_status"] = listing.get("listing_history_count_status")
    if not row["listing_pass"]:
        row["failure_reasons"].append("listing_history_under_20")
    row["eligible_pass"] = bool(row["common_quality_pass"] and row["shape_pass"] and row["liquidity_pass"] and row["listing_pass"])
    return row


def _percentiles(rows: list[dict[str, Any]], key: str, *, higher_is_better: bool = True) -> None:
    if not rows:
        return
    values = pd.Series({index: (float(row[key]) if higher_is_better else -float(row[key])) for index, row in enumerate(rows)}, dtype=float)
    ranks = values.rank(method="average", pct=True)
    for index, row in enumerate(rows):
        row[f"rank_{key}"] = float(ranks[index])


def _sort_a(row: Mapping[str, Any]) -> tuple[float, ...]:
    return (-float(row["score_A"]), -float(row["ret1450"]), -float(row["activity"]), -float(row["range_expansion"]), -float(row["position1450"]), -float(row["amount1450"]))


def _sort_b(row: Mapping[str, Any]) -> tuple[float, ...]:
    return (-float(row["score_B"]), -float(row["prior10_runup"]), float(row["pullback_volume_ratio"]), -float(row["body1450"]), -float(row["position1450"]), -float(row["amount1450"]))


def _select_with_boundary_ties(rows: list[dict[str, Any]], limit: int, sort_key: Any) -> tuple[list[dict[str, Any]], bool]:
    economic = sorted(rows, key=sort_key)
    for index, row in enumerate(economic, start=1):
        row["pool_rank"] = index
    if len(economic) <= limit:
        selected = economic
    else:
        boundary = sort_key(economic[limit - 1])
        selected = [row for row in economic if sort_key(row) <= boundary]
    # Code makes only the serialized display stable after economic membership is fixed.
    display = sorted(selected, key=lambda row: (sort_key(row), row["sec_code"]))
    for index, row in enumerate(display, start=1):
        row["selected_rank"] = index
    return display, len(display) > limit


def rank_channels(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Apply same-pool percentile ranks only after all absolute gates pass."""
    all_rows = list(rows)
    a_pool = [row for row in all_rows if row["a_shape_pass"] and row["liquidity_pass"] and row["listing_pass"] and row["common_quality_pass"]]
    for key in ("ret1450", "activity", "range_expansion", "position1450"):
        _percentiles(a_pool, key)
    for row in a_pool:
        row["channel"] = "A"
        row["score_A"] = float(sum(row[f"rank_{key}"] for key in ("ret1450", "activity", "range_expansion", "position1450")) / 4.0)
    a_selected, a_ties = _select_with_boundary_ties(a_pool, A_LIMIT, _sort_a)
    a_codes = {row["sec_code"] for row in a_pool}
    b_pool = [row for row in all_rows if row["sec_code"] not in a_codes and row["b_shape_pass"] and row["liquidity_pass"] and row["listing_pass"] and row["common_quality_pass"]]
    for key, higher in (("prior10_runup", True), ("pullback_volume_ratio", False), ("body1450", True), ("position1450", True)):
        _percentiles(b_pool, key, higher_is_better=higher)
    for row in b_pool:
        row["channel"] = "B"
        row["score_B"] = float(sum(row[f"rank_{key}"] for key in ("prior10_runup", "pullback_volume_ratio", "body1450", "position1450")) / 4.0)
    b_selected, b_ties = _select_with_boundary_ties(b_pool, B_LIMIT, _sort_b)
    selected_codes = {row["sec_code"] for row in a_selected + b_selected}
    for row in all_rows:
        row["selected"] = row["sec_code"] in selected_codes
        if not row.get("channel"):
            row["channel"] = None
    return {
        "a_pool": a_pool,
        "b_pool": b_pool,
        "a_selected": a_selected,
        "b_selected": b_selected,
        "a_boundary_tie_expanded": a_ties,
        "b_boundary_tie_expanded": b_ties,
    }


def outcome_from_frames(day_stat: Mapping[str, Any] | None, next_bars: pd.DataFrame | None) -> dict[str, Any]:
    """Keep next-open continuation labels separate from gap and fixed exit P&L."""
    buy = (day_stat or {}).get("buy", {})
    if buy.get("status") != "ready":
        return {"outcome_status": "unavailable_buy", "buy_vwap": None, "next_open": None, "intraday_mfe_0930_1000": None, "gap_return": None, "mfe_from_buy": None, "mae_from_buy": None, "sell_vwap": None, "gross_fixed_exit": None, "net30": None}
    buy_vwap = float(buy["vwap"])
    result = {"outcome_status": "ready", "buy_vwap": buy_vwap, "next_open": None, "intraday_mfe_0930_1000": None, "gap_return": None, "mfe_from_buy": None, "mae_from_buy": None, "sell_vwap": None, "gross_fixed_exit": None, "net30": None}
    if next_bars is None:
        result["outcome_status"] = "unavailable_next_day"
        return result
    try:
        opening = validate_minute_window(next_bars, "09:30", "09:31")
        morning = validate_minute_window(next_bars, "09:30", "10:00")
        sell_window = validate_minute_window(next_bars, "10:00", "10:05")
    except TailDataError as exc:
        result["outcome_status"] = _reason("unavailable_window", exc.reason)
        return result
    next_open = float(opening.iloc[0]["open"])
    high = float(morning["high"].max())
    low = float(morning["low"].min())
    result.update({
        "next_open": next_open,
        "intraday_mfe_0930_1000": high / next_open - 1.0,
        "gap_return": next_open / buy_vwap - 1.0,
        "mfe_from_buy": high / buy_vwap - 1.0,
        "mae_from_buy": low / buy_vwap - 1.0,
    })
    sell = vwap_for_window(next_bars, "10:00", "10:05")
    if sell.get("status") != "ready":
        result["outcome_status"] = "unavailable_fixed_exit"
        return result
    gross = float(sell["vwap"]) / buy_vwap - 1.0
    result.update({"sell_vwap": float(sell["vwap"]), "gross_fixed_exit": gross, "net30": gross - 0.003})
    return result


def _load_next_frame(minute_root: str | Path, next_date: str, code: str, cache: dict[tuple[str, str], pd.DataFrame | None]) -> pd.DataFrame | None:
    key = (next_date, code)
    if key not in cache:
        try:
            source, _ = locate_day_source(minute_root, next_date)
            cache[key] = load_minute_session(source, code)
        except TailDataError:
            cache[key] = None
    return cache[key]


def _public_row(row: Mapping[str, Any]) -> dict[str, Any]:
    excluded = {"a_failure_reasons", "b_failure_reasons", "failure_reasons"}
    return {key: value for key, value in row.items() if key not in excluded}


def _write_csv_gz(path: Path, rows: list[Mapping[str, Any]]) -> None:
    columns = sorted({key for row in rows for key in row})
    with path.open("wb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0, filename="") as zipped:
            with io.TextIOWrapper(zipped, encoding="utf-8", newline="") as text:
                writer = csv.DictWriter(text, fieldnames=columns, extrasaction="ignore")
                writer.writeheader()
                for row in rows:
                    writer.writerow({key: "" if value is None else value for key, value in row.items()})


def _fixture_assertions(rows_by_key: Mapping[tuple[str, str], Mapping[str, Any]], daily_top: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    for key, expected in FIXTURES.items():
        row = rows_by_key.get(key)
        date_key, code = key
        actual = row or {}
        checks: dict[str, bool] = {}
        for field, expected_value in expected.items():
            if field == "a_top":
                checks[field] = code in {item["sec_code"] for item in daily_top[date_key]["A"]}
            elif field == "not_selected":
                checks[field] = not bool(actual.get("selected"))
            elif field == "reason_contains":
                checks[field] = any(expected_value in reason for reason in actual.get("b_failure_reasons", []))
            else:
                checks[field] = actual.get(field) == expected_value
        results[f"{date_key}|{code}"] = {"passed": all(checks.values()), "checks": checks}
    return results


def run_canary(minute_root: str | Path, daily_root: str | Path, output_dir: str | Path) -> tuple[dict[str, Any], Path]:
    """Run the five frozen 2024 canary targets and no other target D."""
    calendar, positions = _calendar_for_targets(minute_root, TARGET_DATES)
    identity = _code_identity()
    spec_hash = _spec_hash()
    code_hash = _code_hash(identity)
    run_dir = Path(output_dir) / f"canary-{spec_hash[:12]}-{code_hash[:12]}"
    if run_dir.exists():
        raise TailDataError(f"canary_output_exists:{run_dir.name}")
    run_dir.mkdir(parents=True)
    stats_cache: dict[str, dict[str, dict[str, Any]]] = {}
    source_records: dict[str, dict[str, Any]] = {}
    listing_cache: dict[str, dict[str, Any]] = {}
    next_frames: dict[tuple[str, str], pd.DataFrame | None] = {}
    daily_top: dict[str, Any] = {}
    fixture_rows: dict[str, Any] = {}
    candidate_rows: list[dict[str, Any]] = []
    all_rows_by_key: dict[tuple[str, str], dict[str, Any]] = {}

    for target in TARGET_DATES:
        index = positions[target]
        dependency_dates = calendar[index - 10:index + 2]
        if any(day[:4] != "2024" for day in dependency_dates):
            raise TailDataError("v2_canary_dependency_year_guard")
        for dependency in dependency_dates:
            if dependency not in stats_cache:
                stats_cache[dependency], source_records[dependency] = load_day_v2_statistics(minute_root, dependency)
        day_stats = stats_cache[target]
        prior_dates = calendar[index - 10:index]
        next_date = calendar[index + 1]
        rows: list[dict[str, Any]] = []
        for code in sorted(day_stats):
            prior = [stats_cache[day].get(code) for day in prior_dates]
            # Listing evidence is consumed only after the independent shape and
            # D-1 liquidity gates.  This keeps the all-minute-file universe
            # complete without opening thousands of unrelated daily workbooks.
            provisional_listing = {
                "listing_history_count_status": "at_least_threshold",
                "listing_history_sessions": MIN_HISTORY_SESSIONS,
                "listing_age_source": "deferred_until_shape_and_liquidity_pass",
            }
            row = build_signal_row(code, day_stats.get(code), prior, provisional_listing)
            if row["common_quality_pass"] and row["shape_pass"] and row["liquidity_pass"]:
                listing = development_listing_evidence(
                    daily_root,
                    code,
                    target,
                    minute_visible_sessions=min(index, MIN_HISTORY_SESSIONS),
                    cache=listing_cache,
                )
                row = build_signal_row(code, day_stats.get(code), prior, listing)
            else:
                row["listing_pass"] = False
                row["listing_age_source"] = "not_evaluated_shape_or_liquidity_failed"
                row["listing_history_sessions"] = None
                row["listing_history_count_status"] = "not_evaluated"
                row["eligible_pass"] = False
            row["trade_date"] = target
            rows.append(row)
            all_rows_by_key[(target, code)] = row
        ranked = rank_channels(rows)
        selected = ranked["a_selected"] + ranked["b_selected"]
        for row in selected:
            row["outcome"] = outcome_from_frames(day_stats.get(row["sec_code"]), _load_next_frame(minute_root, next_date, row["sec_code"], next_frames))
            candidate_rows.append(_public_row(row))
        fixture_codes = [code for (day, code) in FIXTURES if day == target]
        for code in fixture_codes:
            fixture = all_rows_by_key[(target, code)]
            fixture["outcome"] = outcome_from_frames(day_stats.get(code), _load_next_frame(minute_root, next_date, code, next_frames))
            fixture_rows[f"{target}|{code}"] = fixture
        daily_top[target] = {
            "A": [_public_row(row) for row in ranked["a_selected"]],
            "B": [_public_row(row) for row in ranked["b_selected"]],
            "a_candidate_count": len(ranked["a_pool"]),
            "b_candidate_count": len(ranked["b_pool"]),
            "a_boundary_tie_expanded": ranked["a_boundary_tie_expanded"],
            "b_boundary_tie_expanded": ranked["b_boundary_tie_expanded"],
            "selected_count": len(selected),
        }
        # No next target can need a day before its own D-10 dependency.
        next_target_index = positions[TARGET_DATES[TARGET_DATES.index(target) + 1]] if target != TARGET_DATES[-1] else None
        if next_target_index is not None:
            keep_from = calendar[next_target_index - 10]
            for cached in list(stats_cache):
                if cached < keep_from:
                    stats_cache.pop(cached)

    assertions = _fixture_assertions(all_rows_by_key, daily_top)
    label = "tnm_v2_1_canary_verified" if all(value["passed"] for value in assertions.values()) else "changes_required_by_frozen_canary"
    summary = {
        "execution_label": label,
        "contract": "TNM-V2-1 fixed five-day canary",
        "targets": list(TARGET_DATES),
        "read_years": ["2024"],
        "spec_hash": spec_hash,
        "code_identity": identity,
        "code_hash": code_hash,
        "capacity_filter_applied": False,
        "st_filter_applied": False,
        "st_status": "unavailable_not_filtered",
        "corporate_action_filter": "unproven_not_applied",
        "source_records": {day: source_records[day] for day in sorted(source_records)},
        "daily_counts": {day: {"a": daily_top[day]["a_candidate_count"], "b": daily_top[day]["b_candidate_count"], "selected": daily_top[day]["selected_count"]} for day in TARGET_DATES},
        "fixture_assertions": assertions,
        "exception_counts": {"invalid_source_files": sum(record["invalid_file_count"] for record in source_records.values())},
    }
    _write_json(run_dir / "canary_summary.json", summary)
    _write_csv_gz(run_dir / "canary_candidates.csv.gz", candidate_rows)
    _write_json(run_dir / "canary_fixture_rows.json", fixture_rows)
    _write_json(run_dir / "canary_daily_top.json", daily_top)
    manifest = {path.name: {"size_bytes": path.stat().st_size, "sha256": _sha256(path)} for path in sorted(run_dir.iterdir()) if path.is_file()}
    _write_json(run_dir / "artifact_manifest.json", manifest)
    return summary, run_dir


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TNM V2 fixed canary only")
    subparsers = parser.add_subparsers(dest="command", required=True)
    canary = subparsers.add_parser("canary")
    canary.add_argument("--minute-root", required=True)
    canary.add_argument("--daily-root", required=True)
    canary.add_argument("--output-dir", required=True)
    args = parser.parse_args(argv)
    if args.command != "canary":
        raise TailDataError("unsupported_v2_command")
    summary, run_dir = run_canary(args.minute_root, args.daily_root, args.output_dir)
    print(f"status={summary['execution_label']} run_dir={run_dir}")
    return 0 if summary["execution_label"] == "tnm_v2_1_canary_verified" else 2


if __name__ == "__main__":
    raise SystemExit(main())
