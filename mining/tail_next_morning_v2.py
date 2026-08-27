"""TNM V2 fixed-rule canary: causal trend-continuation gates and rankings.

This module deliberately reuses V1's source location, parsing and window
validation primitives.  Its readers immediately reduce each CSV to the
additional V2 statistics that V1 did not consume.  It never changes V1.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import date, timedelta, datetime, timezone
import csv
from decimal import Decimal, ROUND_HALF_UP
import gzip
import hashlib
import io
import json
import math
import os
from pathlib import Path, PurePosixPath
import subprocess
import tempfile
import time
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
    _daily_k_input_identity,
    _directory_input_identity,
    _zip_input_identity,
)


TASK_CARD = Path(__file__).resolve().parents[1] / "docs" / "superpowers" / "specs" / "2026-08-26-tail-next-morning-v22-task-cards.md"
DEVELOPMENT_RUNNER_CARD = Path(__file__).resolve().parents[1] / "docs" / "superpowers" / "specs" / "2026-08-26-tail-next-morning-v22-development-runner.md"
V23_TASK_CARD = Path(__file__).resolve().parents[1] / "docs" / "superpowers" / "specs" / "2026-08-27-tail-next-morning-v23-task-cards.md"
TARGET_DATES = ("20240813", "20240826", "20240827", "20240923", "20240926")
MARKET_TARGETS = ("20240923", "20240926")
FIXTURE_TARGETS = (("20240813", "300328"), ("20240826", "300972"), ("20240827", "300972"))
MIN_D1_AMOUNT = 200_000_000.0
A_LIMIT = 10
B_LIMIT = 10
A_RET1450_MIN = 0.04
POSITION1450_MIN = 0.55
A_VWAP_DIST_MIN = 0.01
V22_A_LIMIT = 6
V22_B_LIMIT = 3
V22_MARKET_TARGETS = ("20240923", "20240926")
V23_MARKET_TARGETS = ("20240923", "20240926")
V23_FIXTURES = {
    ("20240923", "300085"): {"prior5_amount_pass_count": 1, "prior5_amount_all_gt_200m": False, "eligible_pass": False},
    ("20240926", "300339"): {"prior5_amount_pass_count": 5, "prior5_amount_all_gt_200m": True},
}
FIXTURES = {
    ("20240923", "300085"): {"a_shape_pass": True, "liquidity_pass": True, "a_top": True},
    ("20240926", "300339"): {"a_shape_pass": True, "liquidity_pass": True, "a_top": True},
    ("20240826", "300972"): {"b_shape_pass": False, "liquidity_pass": False, "not_selected": True},
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
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
        handle.write(_json_bytes(value))
        temporary = Path(handle.name)
    try:
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _spec_hash() -> str:
    text = TASK_CARD.read_text(encoding="utf-8").replace("\r\n", "\n")
    definition = None
    for marker in ("## 4. 执行证据", "## 6. 执行证据", "## 11. 执行证据", "## 12. 执行证据"):
        before, found, _ = text.partition(marker)
        if found:
            definition = before
            break
    if definition is None:
        raise TailDataError("v2_task_card_execution_marker_missing")
    return hashlib.sha256(definition.encode("utf-8")).hexdigest()


def _v22_development_spec_hash() -> str:
    """Bind the approved economics and runner contract, never its evidence."""
    text = DEVELOPMENT_RUNNER_CARD.read_text(encoding="utf-8").replace("\r\n", "\n")
    definition, marker, _ = text.partition("## 7. 执行证据")
    if not marker:
        raise TailDataError("v22_development_execution_marker_missing")
    return _json_identity({"economic_spec_hash": _spec_hash(), "runner_contract": definition})


def _v23_spec_hash() -> str:
    """Bind TNM-V2.3's frozen definition, never its execution evidence."""
    text = V23_TASK_CARD.read_text(encoding="utf-8").replace("\r\n", "\n")
    definition, marker, _ = text.partition("## 10. 执行证据")
    if not marker:
        raise TailDataError("v23_task_card_execution_marker_missing")
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


def _content_identity(records: Mapping[str, Any]) -> str:
    """Stable identity of exactly the source members/workbooks actually read."""
    return hashlib.sha256(_json_bytes({key: dict(value) for key, value in sorted(records.items())})).hexdigest()


def _json_identity(value: Any) -> str:
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def _unit_consumed_input(minute_members: Mapping[str, Mapping[str, str]], daily_workbooks: Mapping[str, Mapping[str, str]]) -> dict[str, Any]:
    """The checkpoint-owned, canonical content proof for exactly one unit."""
    values = {
        "minute_members": {day: dict(sorted(codes.items())) for day, codes in sorted(minute_members.items())},
        "daily_workbooks": {code: dict(value) for code, value in sorted(daily_workbooks.items())},
    }
    return {**values, "unit_consumed_input_identity": _content_identity(values)}


def _unit_minute_members(source_records: Mapping[str, Mapping[str, Any]], dependencies: Mapping[str, set[str] | None]) -> dict[str, dict[str, str]]:
    """Select the members truly consumed by a unit from one-open source records."""
    result: dict[str, dict[str, str]] = {}
    for trade_date, codes in sorted(dependencies.items()):
        available = source_records[trade_date].get("consumed_member_sha256", {})
        # A requested-but-absent member is itself input evidence.  The stable
        # sentinel changes to a content SHA if that member later appears.
        selected = available if codes is None else {code: available.get(code, f"missing:{code}") for code in sorted(codes)}
        result[trade_date] = dict(sorted(selected.items()))
    return result


def _verify_checkpoint_consumed_input(unit: str, result: Mapping[str, Any], source_records: Mapping[str, Mapping[str, Any]]) -> str:
    """Fail closed before a resume or completed fast path trusts an old result."""
    expected = result.get("unit_consumed_input")
    if not isinstance(expected, Mapping):
        raise TailDataError(f"consumed_input_identity_mismatch:{unit}")
    try:
        dependencies = {day: set(codes) for day, codes in expected["minute_members"].items()}
        minute_members = _unit_minute_members(source_records, dependencies)
        daily_workbooks: dict[str, dict[str, str]] = {}
        for code, saved in expected["daily_workbooks"].items():
            path = Path(str(saved["path"]))
            daily_workbooks[str(code)] = {"path": str(path), "sha256": _sha256(path)}
        actual = _unit_consumed_input(minute_members, daily_workbooks)
    except Exception as exc:
        if isinstance(exc, TailDataError) and str(exc).startswith("consumed_input_identity_mismatch:"):
            raise
        raise TailDataError(f"consumed_input_identity_mismatch:{unit}") from exc
    if actual != dict(expected):
        raise TailDataError(f"consumed_input_identity_mismatch:{unit}")
    return str(actual["unit_consumed_input_identity"])


def _standard_limit_rate(code: str) -> float:
    return 0.20 if canonical_code(code).startswith(("300", "301", "688", "689")) else 0.10


def _fen(value: float) -> int:
    return int((Decimal(str(value)) * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _standard_limit_evidence(code: str, d1_close: float, tail_high: float) -> dict[str, Any]:
    """Compare prices in integer fen: no float boundary ambiguity at a limit."""
    rate = _standard_limit_rate(code)
    limit_fen = _fen(float(d1_close) * (1.0 + rate))
    high_fen = _fen(float(tail_high))
    return {
        "limit_rate": rate,
        "limit_up_price": limit_fen / 100.0,
        "tail_high": float(tail_high),
        "tail_limit_touch": high_fen >= limit_fen,
        "st_limit_filter_status": "unproven_standard_board_rule_applied",
    }


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
            "tail_high": float(tail["high"].max()),
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
    try:
        opening = validate_minute_window(bars, "09:30", "09:31")
        morning = validate_minute_window(bars, "09:30", "10:00")
        result["morning_label"] = {
            "status": "ready", "next_open": float(opening.iloc[0]["open"]),
            "high": float(morning["high"].max()), "low": float(morning["low"].min()),
        }
    except TailDataError as exc:
        result["morning_label"] = {"status": "invalid", "reason": exc.reason, "next_open": None, "high": None, "low": None}
    try:
        validate_minute_window(bars, "10:00", "10:05")
        result["fixed_sell"] = vwap_for_window(bars, "10:00", "10:05")
    except TailDataError as exc:
        result["fixed_sell"] = {"status": "invalid_window", "reason": exc.reason, "vwap": None, "amount": None, "volume": None}
    for label, decision_start, sell_start, sell_end in (
        ("a_exit", "10:29", "10:30", "10:35"),
        ("b_exit", "10:59", "11:00", "11:05"),
    ):
        try:
            decision = validate_minute_window(bars, decision_start, sell_start)
            result[label] = {
                "decision_status": "ready",
                "decision_close": float(decision.iloc[-1]["close"]),
                "sell": vwap_for_window(bars, sell_start, sell_end),
                "decision_window": f"{decision_start}-{sell_start}",
                "sell_window": f"{sell_start}-{sell_end}",
            }
        except TailDataError as exc:
            result[label] = {
                "decision_status": "invalid",
                "decision_close": None,
                "sell": {"status": "invalid_window", "reason": exc.reason, "vwap": None, "amount": None, "volume": None},
                "decision_window": f"{decision_start}-{sell_start}",
                "sell_window": f"{sell_start}-{sell_end}",
            }
    try:
        decision = validate_minute_window(bars, "13:04", "13:05")
        result["v23_exit"] = {
            "decision_status": "ready",
            "decision_close": float(decision.iloc[-1]["close"]),
            "sell": vwap_for_window(bars, "13:05", "13:10"),
            "decision_window": "13:04-13:05",
            "sell_window": "13:05-13:10",
        }
    except TailDataError as exc:
        result["v23_exit"] = {
            "decision_status": "invalid",
            "decision_close": None,
            "sell": {"status": "invalid_window", "reason": exc.reason, "vwap": None, "amount": None, "volume": None},
            "decision_window": "13:04-13:05",
            "sell_window": "13:05-13:10",
        }
    return result


def _stats_from_payload(payload: bytes) -> dict[str, Any]:
    try:
        return _v2_statistics_from_frame(pd.read_csv(io.BytesIO(payload)))
    except Exception:
        return {"status": "invalid", "reason": "minute_csv_unreadable"}


def load_day_v2_statistics(
    minute_root: str | Path, trade_date: str, codes: Iterable[str] | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Open one daily container once; optionally parse only named A-share members."""
    key = str(trade_date).replace("-", "")
    if key[:4] not in {"2023", "2024"}:
        raise TailDataError(f"v2_supported_year_guard:{key}")
    source, source_kind = locate_day_source(minute_root, key)
    requested = None if codes is None else {canonical_code(code) for code in codes if is_a_share_code(code)}
    result: dict[str, dict[str, Any]] = {}
    member_digests: dict[str, str] = {}
    duplicates: set[str] = set()
    if source_kind == "zip":
        with zipfile.ZipFile(source) as archive:
            members: dict[str, str] = {}
            for name in sorted(archive.namelist()):
                if name.endswith("/") or PurePosixPath(name).suffix.lower() != ".csv":
                    continue
                code = canonical_code(PurePosixPath(name).stem)
                if not is_a_share_code(code) or (requested is not None and code not in requested):
                    continue
                if code in members:
                    duplicates.add(code)
                else:
                    members[code] = name
            for code, name in members.items():
                if code in duplicates:
                    result[code] = {"status": "invalid", "reason": "ambiguous_minute_code_file"}
                else:
                    payload = archive.read(name)
                    result[code] = _stats_from_payload(payload)
                    if result[code].get("status") == "ready":
                        member_digests[code] = hashlib.sha256(payload).hexdigest()
    else:
        members: dict[str, Path] = {}
        for item in sorted(source.rglob("*.csv")):
            code = canonical_code(item.stem)
            if not is_a_share_code(code) or (requested is not None and code not in requested):
                continue
            if code in members:
                duplicates.add(code)
            else:
                members[code] = item
        for code, item in members.items():
            if code in duplicates:
                result[code] = {"status": "invalid", "reason": "ambiguous_minute_code_file"}
            else:
                payload = item.read_bytes()
                result[code] = _stats_from_payload(payload)
                if result[code].get("status") == "ready":
                    member_digests[code] = hashlib.sha256(payload).hexdigest()
    if requested is not None:
        for code in requested - set(result):
            result[code] = {"status": "invalid", "reason": "minute_code_missing"}
    stat = source.stat()
    return result, {
        "trade_date": key,
        "source_kind": source_kind,
        "size_bytes": stat.st_size if source.is_file() else None,
        "mtime_ns": stat.st_mtime_ns,
        "container_open_count": 1,
        "universe_count": len(result),
        "parsed_code_count": len(result),
        "invalid_file_count": sum(value.get("status") != "ready" for value in result.values()),
        "consumed_member_sha256": member_digests,
        "consumed_member_identity": hashlib.sha256(_json_bytes(member_digests)).hexdigest(),
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
        "st_limit_filter_status": "unproven_standard_board_rule_applied",
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
    prev_ma5 = float(pd.Series([value["history"]["close"] for value in previous[-5:]]).mean())
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
        "prev_ma5": prev_ma5,
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
    metrics.update(_standard_limit_evidence(code, prev_close, float(signal.get("tail_high", signal["high"]))))
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
        (row["ret1450"] >= A_RET1450_MIN, "ret1450<0.04"),
        (row["close1450"] > row["open_D"], "close1450<=open_D"),
        (row["position1450"] >= POSITION1450_MIN, "position1450<0.55"),
        (row["vwap_dist"] >= A_VWAP_DIST_MIN, "vwap_dist<0.01"),
        (row["activity"] >= 1.50, "activity<1.50"),
        (row["range_expansion"] >= 1.50, "range_expansion<1.50"),
        (row["close1450"] > row["prev_ma10"], "close1450<=prev_ma10"),
        (float(row["tail_return"]) >= -0.03, "tail_return<-0.03"),
        (not bool(row["tail_limit_touch"]), "tail_limit_touch"),
        (row["close1450"] >= row["prior10_high"] or (row["prev_ma5"] > row["prev_ma10"] and row["close1450"] > row["prev_ma5"]), "trend_structure_failed"),
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
        (row["position1450"] >= POSITION1450_MIN, "position1450<0.55"),
        (row["body1450"] >= 0.01, "body1450<0.01"),
        (row["range_expansion"] >= 1.00, "range_expansion<1.00"),
        (float(row["tail_return"]) >= -0.03, "tail_return<-0.03"),
        (not bool(row["tail_limit_touch"]), "tail_limit_touch"),
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


def necessary_preselection(code: str, day: Mapping[str, Any] | None, d1: Mapping[str, Any] | None) -> dict[str, Any]:
    """The frozen, deliberately incomplete local gates used only to save history reads."""
    result = {"sec_code": canonical_code(code), "quality_pass": False, "liquidity_pass": False, "a_local_necessary": False, "b_local_necessary": False, "survives": False, "reason": None}
    if not is_a_share_code(code) or not _signal_ready(day) or not _history_ready(d1):
        result["reason"] = "missing_or_invalid_d_or_d1"
        return result
    signal, previous = day["signal"], d1["history"]
    close, high, low = float(signal["close"]), float(signal["high"]), float(signal["low"])
    volume, amount, previous_close = float(signal["volume"]), float(signal["amount"]), float(previous["close"])
    if not all(_finite(value) for value in (close, high, low, volume, amount, previous_close, previous["full_amount"], signal["open"])) or volume <= 0 or amount <= 0 or previous_close <= 0 or high <= low:
        result["reason"] = "non_positive_local_denominator"
        return result
    position = (close - low) / (high - low)
    vwap_dist = close / (amount / volume) - 1.0
    ret = close / previous_close - 1.0
    result.update({"quality_pass": True, "liquidity_pass": float(previous["full_amount"]) > MIN_D1_AMOUNT, "ret1450": ret, "position1450": position, "vwap_dist": vwap_dist})
    result["a_local_necessary"] = ret >= A_RET1450_MIN and position >= POSITION1450_MIN and vwap_dist >= A_VWAP_DIST_MIN
    result["b_local_necessary"] = close > float(signal["open"]) and close > previous_close and position >= POSITION1450_MIN
    result["survives"] = bool(result["liquidity_pass"] and (result["a_local_necessary"] or result["b_local_necessary"]))
    if not result["survives"]:
        result["reason"] = "local_necessary_condition_failed"
    return result


def _percentiles(rows: list[dict[str, Any]], key: str, *, higher_is_better: bool = True) -> None:
    if not rows:
        return
    values = pd.Series({index: (float(row[key]) if higher_is_better else -float(row[key])) for index, row in enumerate(rows)}, dtype=float)
    ranks = values.rank(method="average", pct=True)
    for index, row in enumerate(rows):
        row[f"rank_{key}"] = float(ranks[index])


def _sort_a(row: Mapping[str, Any]) -> tuple[float, ...]:
    return (-float(row["score_A"]), -float(row["ret1450"]), -float(row["volume_score"]), -float(row["range_expansion"]), -float(row["amount1450"]))


def _sort_b(row: Mapping[str, Any]) -> tuple[float, ...]:
    return (-float(row["score_B"]), -float(row["prior10_runup"]), float(row["pullback_volume_ratio"]), -float(row["body1450"]), -float(row["range_expansion"]), -float(row["amount1450"]))


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


def rank_channels(rows: Iterable[dict[str, Any]], *, a_limit: int = A_LIMIT, b_limit: int = B_LIMIT) -> dict[str, Any]:
    """Apply same-pool percentile ranks only after all absolute gates pass."""
    all_rows = list(rows)
    a_pool = [row for row in all_rows if row["a_shape_pass"] and row["liquidity_pass"] and row["listing_pass"] and row["common_quality_pass"]]
    for key in ("ret1450", "activity", "amount1450", "range_expansion"):
        _percentiles(a_pool, key)
    for row in a_pool:
        row["channel"] = "A"
        row["rank_price"] = row["rank_ret1450"]
        row["rank_volatility"] = row["rank_range_expansion"]
        row["volume_score"] = float((row["rank_activity"] + row["rank_amount1450"]) / 2.0)
        row["score_A"] = float((row["rank_price"] + row["volume_score"] + row["rank_volatility"]) / 3.0)
    a_selected, a_ties = _select_with_boundary_ties(a_pool, a_limit, _sort_a)
    a_codes = {row["sec_code"] for row in a_pool}
    b_pool = [row for row in all_rows if row["sec_code"] not in a_codes and row["b_shape_pass"] and row["liquidity_pass"] and row["listing_pass"] and row["common_quality_pass"]]
    for key, higher in (("prior10_runup", True), ("pullback_volume_ratio", False), ("body1450", True), ("range_expansion", True)):
        _percentiles(b_pool, key, higher_is_better=higher)
    for row in b_pool:
        row["channel"] = "B"
        row["score_B"] = float(sum(row[f"rank_{key}"] for key in ("prior10_runup", "pullback_volume_ratio", "body1450", "range_expansion")) / 4.0)
    b_selected, b_ties = _select_with_boundary_ties(b_pool, b_limit, _sort_b)
    selected_codes = {row["sec_code"] for row in a_selected + b_selected}
    for row in all_rows:
        row["selected"] = row["sec_code"] in selected_codes
        row["selected_top10"] = row["selected"]
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


def _gate_counts(rows: Iterable[Mapping[str, Any]], ranked: Mapping[str, Any], preselection: Mapping[str, Any]) -> dict[str, Any]:
    """Runtime counts: predicate failures overlap; first failures are exclusive."""
    evaluated = list(rows)
    a_fail: defaultdict[str, int] = defaultdict(int)
    b_fail: defaultdict[str, int] = defaultdict(int)
    first: defaultdict[str, int] = defaultdict(int)
    overlap = 0
    for row in evaluated:
        for reason in row.get("a_failure_reasons", []):
            a_fail[str(reason)] += 1
        for reason in row.get("b_failure_reasons", []):
            b_fail[str(reason)] += 1
        a_qualified = bool(row.get("a_shape_pass") and row.get("liquidity_pass") and row.get("listing_pass") and row.get("common_quality_pass"))
        b_qualified = bool(row.get("b_shape_pass") and row.get("liquidity_pass") and row.get("listing_pass") and row.get("common_quality_pass"))
        listing_evaluated = row.get("listing_age_source") != "not_evaluated_shape_or_liquidity_failed"
        if a_qualified and b_qualified:
            overlap += 1
        if not row.get("common_quality_pass"):
            first["common_quality"] += 1
        elif not row.get("liquidity_pass"):
            first["d1_liquidity"] += 1
        elif listing_evaluated and not row.get("listing_pass"):
            first["listing"] += 1
        elif a_qualified:
            first["eligible_A"] += 1
        elif b_qualified:
            first["eligible_B"] += 1
        else:
            first["both_channel_shape_failed"] += 1
    predicate = {
        "A": dict(sorted(a_fail.items())),
        "B": dict(sorted(b_fail.items())),
        "common_quality_failed": sum(not bool(row.get("common_quality_pass")) for row in evaluated),
        "d1_liquidity_failed": sum(not bool(row.get("liquidity_pass")) for row in evaluated),
        "listing_failed": sum(
            row.get("listing_age_source") != "not_evaluated_shape_or_liquidity_failed" and not bool(row.get("listing_pass"))
            for row in evaluated
        ),
        "a_b_overlap_excluded_from_B": overlap,
        "tail_limit_touch_before_channel": sum(bool(row.get("tail_limit_touch")) for row in evaluated if row.get("common_quality_pass")),
    }
    return {
        "counting_contract": "predicate_fail_counts overlap; first_failure_counts are exclusive and first_failure_total equals evaluated_final_gate_rows",
        "predicate_fail_counts": predicate,
        "first_failure_counts": dict(sorted(first.items())),
        "first_failure_total": sum(first.values()),
        "evaluated_final_gate_rows": len(evaluated),
        "universe": int(preselection["universe"]),
        "preselection_survivors": int(preselection["survivors"]),
        "A_eligible": len(ranked["a_pool"]),
        "B_eligible": len(ranked["b_pool"]),
        "A_top10": len(ranked["a_selected"]),
        "B_top10": len(ranked["b_selected"]),
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
    morning_ok, sell_ok = False, False
    try:
        opening = validate_minute_window(next_bars, "09:30", "09:31")
        morning = validate_minute_window(next_bars, "09:30", "10:00")
        next_open = float(opening.iloc[0]["open"])
        high, low = float(morning["high"].max()), float(morning["low"].min())
        result.update({"next_open": next_open, "intraday_mfe_0930_1000": high / next_open - 1.0, "gap_return": next_open / buy_vwap - 1.0, "mfe_from_buy": high / buy_vwap - 1.0, "mae_from_buy": low / buy_vwap - 1.0})
        morning_ok = True
    except TailDataError:
        pass
    sell = vwap_for_window(next_bars, "10:00", "10:05")
    if sell.get("status") == "ready":
        gross = float(sell["vwap"]) / buy_vwap - 1.0
        result.update({"sell_vwap": float(sell["vwap"]), "gross_fixed_exit": gross, "net30": gross - 0.003})
        sell_ok = True
    result["outcome_status"] = "ready" if morning_ok and sell_ok else "unavailable_morning_label" if sell_ok else "unavailable_fixed_exit" if morning_ok else "unavailable_morning_and_fixed_exit"
    return result


def outcome_from_statistics(day_stat: Mapping[str, Any] | None, next_stat: Mapping[str, Any] | None) -> dict[str, Any]:
    """Same outcome contract as frame mode, using an already selectively parsed D+1 member."""
    buy = (day_stat or {}).get("buy", {})
    empty = {"outcome_status": "unavailable_buy", "buy_vwap": None, "next_open": None, "intraday_mfe_0930_1000": None, "gap_return": None, "mfe_from_buy": None, "mae_from_buy": None, "sell_vwap": None, "gross_fixed_exit": None, "net30": None}
    if buy.get("status") != "ready":
        return empty
    result = {**empty, "outcome_status": "ready", "buy_vwap": float(buy["vwap"])}
    if not next_stat:
        result["outcome_status"] = "unavailable_next_day"
        return result
    morning, sell = next_stat.get("morning_label", {}), next_stat.get("fixed_sell", {})
    morning_ok, sell_ok = morning.get("status") == "ready", sell.get("status") == "ready"
    buy_vwap = float(buy["vwap"])
    if morning_ok:
        next_open, high, low = float(morning["next_open"]), float(morning["high"]), float(morning["low"])
        result.update({"next_open": next_open, "intraday_mfe_0930_1000": high / next_open - 1.0, "gap_return": next_open / buy_vwap - 1.0, "mfe_from_buy": high / buy_vwap - 1.0, "mae_from_buy": low / buy_vwap - 1.0})
    if sell_ok:
        gross = float(sell["vwap"]) / buy_vwap - 1.0
        result.update({"sell_vwap": float(sell["vwap"]), "gross_fixed_exit": gross, "net30": gross - 0.003})
    result["outcome_status"] = "ready" if morning_ok and sell_ok else "unavailable_morning_label" if sell_ok else "unavailable_fixed_exit" if morning_ok else "unavailable_morning_and_fixed_exit"
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
    return dict(row)


def _write_csv_gz(path: Path, rows: list[Mapping[str, Any]]) -> None:
    columns = sorted({key for row in rows for key in row})
    with path.open("wb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0, filename="") as zipped:
            with io.TextIOWrapper(zipped, encoding="utf-8", newline="") as text:
                writer = csv.DictWriter(text, fieldnames=columns, extrasaction="ignore")
                writer.writeheader()
                for row in rows:
                    writer.writerow({key: "" if value is None else json.dumps(value, ensure_ascii=False, sort_keys=True) if isinstance(value, (dict, list)) else value for key, value in row.items()})


def _fixture_assertions(rows_by_key: Mapping[tuple[str, str], Mapping[str, Any]], daily_top: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    for key, expected in FIXTURES.items():
        row = rows_by_key.get(key)
        date_key, code = key
        actual = row or {}
        checks: dict[str, bool] = {}
        for field, expected_value in expected.items():
            if field == "a_top":
                checks[field] = code in {item["sec_code"] for item in daily_top.get(date_key, {}).get("A", [])}
            elif field == "not_selected":
                checks[field] = not bool(actual.get("selected"))
            elif field == "reason_contains":
                checks[field] = any(expected_value in reason for reason in actual.get("b_failure_reasons", []))
            else:
                checks[field] = actual.get(field) == expected_value
        results[f"{date_key}|{code}"] = {"passed": all(checks.values()), "checks": checks}
    return results


def _current_commit() -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=Path(__file__).resolve().parents[1], check=True, capture_output=True, text=True).stdout.strip()


def _canary_input_manifest(minute_root: str | Path, daily_root: str | Path, calendar: list[str], positions: Mapping[str, int]) -> dict[str, Any]:
    needed = set()
    for target in TARGET_DATES:
        index = positions[target]
        needed.update(calendar[index - 10:index + 2])
    records = []
    for trade_date in sorted(needed):
        source, kind = locate_day_source(minute_root, trade_date)
        stat = source.stat()
        records.append({"trade_date": trade_date, "source_kind": kind, "path": str(source), "size_bytes": stat.st_size if source.is_file() else None, "mtime_ns": stat.st_mtime_ns})
    return {"minute_containers": records, "daily_root": str(Path(daily_root)), "targets": list(TARGET_DATES), "market_targets": list(MARKET_TARGETS)}


def _prepare_canary_run(output_dir: str | Path, manifest: Mapping[str, Any], *, resume_run_id: str | None) -> tuple[Path, dict[str, Any]]:
    identity = {"mode": "tnm-v2-r1-diagnostic", "spec_hash": _spec_hash(), "base_commit": _current_commit(), "source_blob_identity": _code_identity(), "input_manifest_hash": hashlib.sha256(_json_bytes(manifest)).hexdigest()}
    run_hash = hashlib.sha256(_json_bytes(identity)).hexdigest()
    calculated = f"diagnostic-{identity['spec_hash'][:12]}-{run_hash[:12]}"
    if resume_run_id is not None and resume_run_id != calculated:
        raise TailDataError("resume_identity_mismatch")
    run_dir = Path(output_dir) / calculated
    run_manifest = {**identity, "run_hash": run_hash, "run_id": calculated, "input_manifest": manifest}
    path = run_dir / "run_manifest.json"
    if path.exists():
        if resume_run_id is None:
            raise TailDataError("canary_output_exists_requires_explicit_resume")
        if json.loads(path.read_text(encoding="utf-8")).get("run_hash") != run_hash:
            raise TailDataError("resume_hash_mismatch")
    else:
        if resume_run_id is not None:
            raise TailDataError("resume_run_manifest_missing")
        _write_json(path, run_manifest)
    return run_dir, run_manifest


def _checkpoint(run_dir: Path, run_hash: str, unit: str, result: Mapping[str, Any]) -> None:
    _write_json(run_dir / "checkpoints" / f"{unit.replace('|', '_').replace(':', '_')}.json", {"run_hash": run_hash, "unit": unit, "result": result})


def _verify_succeeded_artifacts(run_dir: Path, completion: Mapping[str, Any]) -> None:
    artifact_path = run_dir / "artifact_manifest.json"
    expected = json.loads(artifact_path.read_text(encoding="utf-8"))
    actual = {
        path.name: {"size_bytes": path.stat().st_size, "sha256": _sha256(path)}
        for path in sorted(run_dir.iterdir())
        if path.is_file() and path.name not in {"artifact_manifest.json", "completion.json"}
    }
    if completion.get("artifact_manifest_sha256") != _sha256(artifact_path) or actual != expected:
        raise TailDataError("succeeded_artifact_manifest_mismatch")


def _final_row(code: str, target: str, index: int, day: Mapping[str, Any] | None, prior: list[Mapping[str, Any] | None], daily_root: str | Path, listing_cache: dict[str, dict[str, Any]], daily_consumed: dict[str, dict[str, str]] | None = None) -> dict[str, Any]:
    provisional = {"listing_history_count_status": "at_least_threshold", "listing_history_sessions": MIN_HISTORY_SESSIONS, "listing_age_source": "deferred_until_shape_and_liquidity_pass"}
    row = build_signal_row(code, day, prior, provisional)
    if row["common_quality_pass"] and row["shape_pass"] and row["liquidity_pass"]:
        listing = development_listing_evidence(daily_root, code, target, minute_visible_sessions=min(index, MIN_HISTORY_SESSIONS), cache=listing_cache)
        workbook = Path(daily_root) / f"{canonical_code(code)}.xlsx"
        if daily_consumed is not None and workbook.exists():
            daily_consumed[canonical_code(code)] = {"path": str(workbook), "sha256": _sha256(workbook)}
        row = build_signal_row(code, day, prior, listing)
    else:
        row.update({"listing_pass": False, "listing_age_source": "not_evaluated_shape_or_liquidity_failed", "listing_history_sessions": None, "listing_history_count_status": "not_evaluated", "eligible_pass": False})
    row["trade_date"] = target
    return row


def run_diagnostic(minute_root: str | Path, daily_root: str | Path, output_dir: str | Path, *, resume_run_id: str | None = None) -> tuple[dict[str, Any], Path]:
    """R1's two full-market scans plus three frozen single-code fixtures."""
    started = time.monotonic()
    calendar, positions = _calendar_for_targets(minute_root, TARGET_DATES)
    input_manifest = _canary_input_manifest(minute_root, daily_root, calendar, positions)
    run_dir, run_manifest = _prepare_canary_run(output_dir, input_manifest, resume_run_id=resume_run_id)
    run_hash = run_manifest["run_hash"]
    progress_path, completion_path = run_dir / "progress.json", run_dir / "completion.json"
    preserve_existing_terminal = completion_path.exists()
    stats_cache: dict[str, dict[str, dict[str, Any]]] = {}
    source_records: dict[str, dict[str, Any]] = {}
    listing_cache: dict[str, dict[str, Any]] = {}

    def load_once(trade_date: str, codes: Iterable[str] | None) -> dict[str, dict[str, Any]]:
        requested = None if codes is None else {canonical_code(code) for code in codes}
        if trade_date in stats_cache:
            if requested is not None and not requested.issubset(stats_cache[trade_date]):
                raise TailDataError(f"container_reopen_forbidden:{trade_date}")
            return stats_cache[trade_date]
        values, record = load_day_v2_statistics(minute_root, trade_date, requested)
        stats_cache[trade_date], source_records[trade_date] = values, record
        return values

    try:
        market_pre: dict[str, dict[str, dict[str, Any]]] = {}
        for target in MARKET_TARGETS:
            index = positions[target]
            day, d1 = load_once(target, None), load_once(calendar[index - 1], None)
            market_pre[target] = {code: necessary_preselection(code, day.get(code), d1.get(code)) for code in day}
        required_history: dict[str, set[str]] = defaultdict(set)
        evaluation_codes: dict[str, set[str]] = {}
        for target in MARKET_TARGETS:
            index = positions[target]
            fixture_codes = {code for (day, code) in FIXTURES if day == target}
            survivors = {code for code, proof in market_pre[target].items() if proof["survives"]}
            evaluation_codes[target] = survivors | fixture_codes
            for history_date in calendar[index - 10:index - 1]:
                required_history[history_date].update(evaluation_codes[target])
            # D+1 may also be another full-market target's historical day
            # (20240924 in the frozen pair).  Reserve every possible outcome
            # consumer now, before the single open for that shared container.
            required_history[calendar[index + 1]].update(evaluation_codes[target])
        for target, code in FIXTURE_TARGETS:
            index = positions[target]
            # Fixtures are single-code, but their D/D-1 can be another
            # fixture's history.  Plan the complete D-10..D+1 union before
            # opening any of these containers so each date opens once.
            for dependency_date in calendar[index - 10:index + 2]:
                required_history[dependency_date].add(code)
        for history_date, codes in sorted(required_history.items()):
            load_once(history_date, codes)

        completed: dict[str, Any] = {}
        for path in sorted((run_dir / "checkpoints").glob("*.json")) if (run_dir / "checkpoints").exists() else []:
            saved = json.loads(path.read_text(encoding="utf-8"))
            if saved.get("run_hash") != run_hash:
                raise TailDataError("checkpoint_hash_mismatch")
            _verify_checkpoint_consumed_input(str(saved["unit"]), saved["result"], source_records)
            completed[saved["unit"]] = saved["result"]
        if completion_path.exists():
            completion = json.loads(completion_path.read_text(encoding="utf-8"))
            expected_units = {f"market:{target}" for target in MARKET_TARGETS} | {f"fixture:{target}|{code}" for target, code in FIXTURE_TARGETS}
            if completion.get("status") == "SUCCEEDED":
                if completion.get("run_hash") != run_hash:
                    raise TailDataError("completion_hash_mismatch")
                if set(completed) != expected_units:
                    raise TailDataError("succeeded_checkpoint_set_mismatch")
                verified_unit_identities = {
                    unit: _verify_checkpoint_consumed_input(unit, result, source_records)
                    for unit, result in sorted(completed.items())
                }
                summary = json.loads((run_dir / "diagnostic_summary.json").read_text(encoding="utf-8"))
                if (
                    summary.get("run_hash") != run_hash
                    or summary.get("verified_checkpoint_consumed_identities") != verified_unit_identities
                    or summary.get("consumed_input_identity") != hashlib.sha256(_json_bytes(verified_unit_identities)).hexdigest()
                ):
                    raise TailDataError("succeeded_summary_identity_mismatch")
                _verify_succeeded_artifacts(run_dir, completion)
                return summary, run_dir
        _write_json(progress_path, {"run_hash": run_hash, "stage": "initializing", "completed_units": [], "elapsed_seconds": 0.0, "exception_counts": {}})
        daily_top: dict[str, Any] = {}
        fixture_rows: dict[str, Any] = {}
        eligible_rows: list[dict[str, Any]] = []
        excluded_limit_touch: list[dict[str, Any]] = []
        unit_survivors: dict[str, Any] = {}

        def progress(stage: str) -> None:
            _write_json(progress_path, {"run_hash": run_hash, "stage": stage, "completed_units": sorted(completed), "survivor_counts": unit_survivors, "elapsed_seconds": round(time.monotonic() - started, 6), "exception_counts": {"invalid_source_files": sum(record["invalid_file_count"] for record in source_records.values())}})

        for target in MARKET_TARGETS:
            unit = f"market:{target}"
            if unit in completed:
                result = completed[unit]
            else:
                index = positions[target]
                day, d1 = stats_cache[target], stats_cache[calendar[index - 1]]
                rows = []
                unit_daily_consumed: dict[str, dict[str, str]] = {}
                for code in sorted(evaluation_codes[target]):
                    prior = [stats_cache[history_date].get(code) for history_date in calendar[index - 10:index - 1]] + [d1.get(code)]
                    rows.append(_final_row(code, target, index, day.get(code), prior, daily_root, listing_cache, unit_daily_consumed))
                ranked = rank_channels(rows)
                selected = ranked["a_selected"] + ranked["b_selected"]
                pool = ranked["a_pool"] + ranked["b_pool"]
                outcome_codes = {row["sec_code"] for row in pool} | {code for (date_key, code) in FIXTURES if date_key == target}
                next_stats = load_once(calendar[index + 1], outcome_codes)
                for row in pool:
                    row["outcome"] = outcome_from_statistics(day.get(row["sec_code"]), next_stats.get(row["sec_code"]))
                fixture_subset = {}
                for code in sorted({code for (date_key, code) in FIXTURES if date_key == target}):
                    row = next(row for row in rows if row["sec_code"] == code)
                    row["outcome"] = outcome_from_statistics(day.get(code), next_stats.get(code))
                    fixture_subset[f"{target}|{code}"] = row
                top = {"A": [_public_row(row) for row in ranked["a_selected"]], "B": [_public_row(row) for row in ranked["b_selected"]], "a_candidate_count": len(ranked["a_pool"]), "b_candidate_count": len(ranked["b_pool"]), "a_boundary_tie_expanded": ranked["a_boundary_tie_expanded"], "b_boundary_tie_expanded": ranked["b_boundary_tie_expanded"], "selected_count": len(selected)}
                touch_rows = []
                for code, value in day.items():
                    if _signal_ready(value) and _history_ready(d1.get(code)):
                        evidence = _standard_limit_evidence(code, float(d1[code]["history"]["close"]), float(value["signal"].get("tail_high", value["signal"]["high"])))
                        if evidence["tail_limit_touch"]:
                            touch_rows.append({"trade_date": target, "sec_code": code, "failure_reason": "tail_limit_touch", **evidence})
                preselection = {"universe": len(day), "survivors": sum(proof["survives"] for proof in market_pre[target].values()), "evaluated": len(rows)}
                unit_dependencies: dict[str, set[str] | None] = {target: None, calendar[index - 1]: None}
                unit_dependencies.update({history_date: set(evaluation_codes[target]) for history_date in calendar[index - 10:index - 1]})
                unit_dependencies[calendar[index + 1]] = set(outcome_codes)
                result = {"daily_top": top, "eligible_rows": [_public_row(row) for row in pool], "fixture_rows": fixture_subset, "excluded_limit_touch": touch_rows, "preselection": preselection, "gate_counts": _gate_counts(rows, ranked, preselection), "unit_consumed_input": _unit_consumed_input(_unit_minute_members(source_records, unit_dependencies), unit_daily_consumed)}
                _checkpoint(run_dir, run_hash, unit, result)
                completed[unit] = result
            daily_top[target] = result["daily_top"]
            eligible_rows.extend(result["eligible_rows"])
            excluded_limit_touch.extend(result.get("excluded_limit_touch", []))
            fixture_rows.update(result["fixture_rows"])
            unit_survivors[unit] = result["preselection"]
            progress(unit)

        for target, code in FIXTURE_TARGETS:
            unit = f"fixture:{target}|{code}"
            if unit in completed:
                result = completed[unit]
            else:
                index = positions[target]
                day, d1 = stats_cache[target], stats_cache[calendar[index - 1]]
                prior = [stats_cache[history_date].get(code) for history_date in calendar[index - 10:index - 1]] + [d1.get(code)]
                unit_daily_consumed: dict[str, dict[str, str]] = {}
                row = _final_row(code, target, index, day.get(code), prior, daily_root, listing_cache, unit_daily_consumed)
                next_stats = load_once(calendar[index + 1], {code})
                row["outcome"] = outcome_from_statistics(day.get(code), next_stats.get(code))
                row["selected"] = False
                row["channel"] = None
                unit_dependencies = {dependency_date: {code} for dependency_date in calendar[index - 10:index + 2]}
                result = {"fixture_row": row, "preselection": necessary_preselection(code, day.get(code), d1.get(code)), "unit_consumed_input": _unit_consumed_input(_unit_minute_members(source_records, unit_dependencies), unit_daily_consumed)}
                _checkpoint(run_dir, run_hash, unit, result)
                completed[unit] = result
            fixture_rows[f"{target}|{code}"] = result["fixture_row"]
            unit_survivors[unit] = result["preselection"]
            progress(unit)

        assertions = _fixture_assertions({tuple(key.split("|")): value for key, value in fixture_rows.items()}, daily_top)
        expected_touches = {
            ("20240923", code) for code in ("600619", "002405", "600203")
        } | {
            ("20240926", code) for code in ("002583", "300100", "600208")
        }
        observed_touches = {(str(row["trade_date"]), str(row["sec_code"])) for row in excluded_limit_touch}
        for target, code in sorted(expected_touches):
            assertions[f"{target}|{code}"] = {"passed": (target, code) in observed_touches, "checks": {"tail_limit_touch": (target, code) in observed_touches}}
        label = "tnm_v2_r1_diagnostic_ready" if all(value["passed"] for value in assertions.values()) else "diagnostic_changes_required"
        verified_unit_identities = {
            unit: _verify_checkpoint_consumed_input(unit, result, source_records)
            for unit, result in sorted(completed.items())
        }
        run_manifest["consumed_input_identity"] = hashlib.sha256(_json_bytes(verified_unit_identities)).hexdigest()
        run_manifest["verified_checkpoint_consumed_identities"] = verified_unit_identities
        _write_json(run_dir / "run_manifest.json", run_manifest)
        summary = {"execution_label": label, "contract": "TNM-V2-R1R two-market-three-fixture diagnostic", "targets": list(TARGET_DATES), "market_targets": list(MARKET_TARGETS), "fixture_targets": [f"{day}|{code}" for day, code in FIXTURE_TARGETS], "read_years": ["2024"], "spec_hash": run_manifest["spec_hash"], "base_commit": run_manifest["base_commit"], "source_blob_identity": run_manifest["source_blob_identity"], "run_hash": run_hash, "input_manifest_hash": run_manifest["input_manifest_hash"], "consumed_input_identity": run_manifest["consumed_input_identity"], "verified_checkpoint_consumed_identities": verified_unit_identities, "capacity_filter_applied": False, "st_filter_applied": False, "st_status": "unavailable_not_filtered", "st_limit_filter_status": "unproven_standard_board_rule_applied", "corporate_action_filter": "unproven_not_applied", "source_records": {day: source_records[day] for day in sorted(source_records)}, "daily_counts": {day: {"a": daily_top[day]["a_candidate_count"], "b": daily_top[day]["b_candidate_count"], "selected": daily_top[day]["selected_count"]} for day in MARKET_TARGETS}, "gate_counts": {day: completed[f"market:{day}"]["gate_counts"] for day in MARKET_TARGETS}, "excluded_limit_touch_count": len(excluded_limit_touch), "fixture_assertions": assertions, "performance": {"elapsed_seconds": round(time.monotonic() - started, 6), "container_open_total": sum(record["container_open_count"] for record in source_records.values())}, "exception_counts": {"invalid_source_files": sum(record["invalid_file_count"] for record in source_records.values())}, "read_2025_2026": False, "e_drive_written": False}
        _write_json(run_dir / "diagnostic_summary.json", summary)
        _write_csv_gz(run_dir / "diagnostic_eligible_pool.csv.gz", eligible_rows)
        _write_csv_gz(run_dir / "diagnostic_excluded_limit_touch.csv.gz", excluded_limit_touch)
        _write_json(run_dir / "diagnostic_fixture_rows.json", fixture_rows)
        _write_json(run_dir / "diagnostic_daily_top10.json", daily_top)
        artifact_manifest = {path.name: {"size_bytes": path.stat().st_size, "sha256": _sha256(path)} for path in sorted(run_dir.iterdir()) if path.is_file() and path.name not in {"artifact_manifest.json", "completion.json"}}
        _write_json(run_dir / "artifact_manifest.json", artifact_manifest)
        _write_json(completion_path, {"status": "SUCCEEDED", "run_hash": run_hash, "execution_label": label, "artifact_manifest_sha256": _sha256(run_dir / "artifact_manifest.json")})
        return summary, run_dir
    except KeyboardInterrupt:
        if not preserve_existing_terminal:
            _write_json(completion_path, {"status": "CANCELLED", "run_hash": run_hash})
        raise


    except Exception as exc:
        if not preserve_existing_terminal:
            _write_json(completion_path, {"status": "FAILED", "run_hash": run_hash, "reason": type(exc).__name__, "detail": str(exc)})
        raise


# Compatibility for the V2-1F synthetic harness.  The production CLI exposes
# only the R1 diagnostic command below.
run_canary = run_diagnostic


def v22_impulse(prior: Iterable[Mapping[str, Any] | None]) -> dict[str, Any]:
    """Choose one D-5..D-1 impulse; all ranking fields share that day."""
    values = list(prior)
    candidates: list[dict[str, Any]] = []
    if len(values) != 10 or not all(_history_ready(value) for value in values):
        return {"impulse_pass": False, "impulse_day": None, "impulse_return": None, "impulse_amount_ratio": None}
    for index in range(5, 10):
        history = values[index]["history"]
        prior_close = float(values[index - 1]["history"]["close"])
        denominator = float(pd.Series([values[item]["history"]["full_amount"] for item in range(index - 3, index)]).median())
        if prior_close <= 0 or denominator <= 0:
            continue
        impulse_return = float(history["close"]) / prior_close - 1.0
        impulse_amount_ratio = float(history["full_amount"]) / denominator
        if impulse_return >= .05 and impulse_amount_ratio >= 1.50:
            candidates.append({"impulse_day": f"D-{10 - index}", "impulse_return": impulse_return, "impulse_amount_ratio": impulse_amount_ratio, "_index": index})
    if not candidates:
        return {"impulse_pass": False, "impulse_day": None, "impulse_return": None, "impulse_amount_ratio": None}
    chosen = sorted(candidates, key=lambda value: (-value["impulse_return"], -value["impulse_amount_ratio"], -value["_index"]))[0]
    return {key: value for key, value in chosen.items() if key != "_index"} | {"impulse_pass": True}


def _v22_a_shape(row: dict[str, Any]) -> None:
    if not row["common_quality_pass"]:
        row["a_failure_reasons"] = ["common_quality_failed"]
        return
    gates = (
        (row["ret1450"] >= A_RET1450_MIN, "ret1450<0.04"),
        (row["close1450"] > row["open_D"], "close1450<=open_D"),
        (row["position1450"] >= POSITION1450_MIN, "position1450<0.55"),
        (row["vwap_dist"] >= A_VWAP_DIST_MIN, "vwap_dist<0.01"),
        (row["range_expansion"] >= 1.50, "range_expansion<1.50"),
        (row["close1450"] > row["prev_ma10"], "close1450<=prev_ma10"),
        (float(row["tail_return"]) >= -.03, "tail_return<-0.03"),
        (not bool(row["tail_limit_touch"]), "tail_limit_touch"),
        (row["close1450"] >= row["prior10_high"] or (row["prev_ma5"] > row["prev_ma10"] and row["close1450"] > row["prev_ma5"]), "trend_structure_failed"),
    )
    row["a_failure_reasons"] = [reason for passed, reason in gates if not passed]
    row["a_shape_pass"] = not row["a_failure_reasons"]


def _v22_b_shape(row: dict[str, Any]) -> None:
    if not row["common_quality_pass"]:
        row["b_failure_reasons"] = ["common_quality_failed"]
        return
    gates = (
        (bool(row["impulse_pass"]), "impulse_missing"),
        (row["prior10_runup"] >= .15, "prior10_runup<0.15"),
        (-.15 <= row["drawdown"] <= -.03, "drawdown_outside_-0.15_to_-0.03"),
        (row["close1450"] > row["prev_ma10"], "close1450<=prev_ma10"),
        (row["pullback_volume_ratio"] <= .80, "pullback_volume_ratio>0.80"),
        (row["position1450"] >= POSITION1450_MIN, "position1450<0.55"),
        (row["body1450"] >= -.03, "body1450<-0.03"),
        (float(row["tail_return"]) >= -.03, "tail_return<-0.03"),
        (not bool(row["tail_limit_touch"]), "tail_limit_touch"),
    )
    row["b_failure_reasons"] = [reason for passed, reason in gates if not passed]
    row["b_shape_pass"] = not row["b_failure_reasons"]


def build_v22_signal_row(code: str, day: Mapping[str, Any] | None, prior: Iterable[Mapping[str, Any] | None], listing: Mapping[str, Any]) -> dict[str, Any]:
    row = _base_row(code, day, list(prior))
    row.update(v22_impulse(prior))
    _v22_a_shape(row)
    _v22_b_shape(row)
    row["shape_pass"] = bool(row["a_shape_pass"] or row["b_shape_pass"])
    row["listing_pass"] = listing.get("listing_history_count_status") == "at_least_threshold" or int(listing.get("listing_history_sessions") or 0) >= MIN_HISTORY_SESSIONS
    row["listing_age_source"] = listing.get("listing_age_source")
    row["listing_history_sessions"] = listing.get("listing_history_sessions")
    row["listing_history_count_status"] = listing.get("listing_history_count_status")
    row["eligible_pass"] = bool(row["common_quality_pass"] and row["shape_pass"] and row["liquidity_pass"] and row["listing_pass"])
    return row


def _v23_apply_prior5_liquidity(row: dict[str, Any], prior: Iterable[Mapping[str, Any] | None]) -> None:
    """Replace V2.2's D-1 gate with V2.3's five strict full-session amounts."""
    values = list(prior)[-5:]
    amounts: list[float | None] = []
    for value in values:
        history = (value or {}).get("history", {}) if isinstance(value, Mapping) else {}
        amount = history.get("full_amount")
        amounts.append(float(amount) if _finite(amount) and float(amount) > 0 else None)
    valid = len(values) == 5 and all(amount is not None for amount in amounts)
    pass_count = sum(amount is not None and amount > MIN_D1_AMOUNT for amount in amounts)
    row["prior5_amounts"] = amounts
    row["prior5_amount_pass_count"] = pass_count
    row["prior5_amount_min"] = min(amount for amount in amounts if amount is not None) if valid else None
    row["prior5_amount_all_gt_200m"] = bool(valid and pass_count == 5)
    row["liquidity_pass"] = row["prior5_amount_all_gt_200m"]
    row["failure_reasons"] = [reason for reason in row["failure_reasons"] if reason != "d1_amount_not_strictly_above_200m"]
    if not row["liquidity_pass"]:
        row["failure_reasons"].append("prior5_amount_not_all_strictly_above_200m")


def build_v23_signal_row(code: str, day: Mapping[str, Any] | None, prior: Iterable[Mapping[str, Any] | None], listing: Mapping[str, Any]) -> dict[str, Any]:
    """V2.3 keeps V2.2 A/B shapes and scores, changing only common liquidity."""
    history = list(prior)
    row = build_v22_signal_row(code, day, history, listing)
    _v23_apply_prior5_liquidity(row, history)
    row["eligible_pass"] = bool(row["common_quality_pass"] and row["shape_pass"] and row["liquidity_pass"] and row["listing_pass"])
    return row


def _v22_sort_a(row: Mapping[str, Any]) -> tuple[float, ...]:
    return (-float(row["score_A"]), -float(row["ret1450"]), -float(row["volume_score"]), -float(row["range_expansion"]), -float(row["amount1450"]), -float(row["vwap_dist"]), -float(row["position1450"]), -float(row["tail_return"]))


def _v22_sort_b(row: Mapping[str, Any]) -> tuple[float, ...]:
    return (-float(row["score_B"]), -float(row["impulse_return"]), -float(row["impulse_amount_ratio"]), float(row["pullback_volume_ratio"]), -float(row["position1450"]), -float(row["body1450"]), -float(row["tail_return"]), -float(row["amount1450"]))


def rank_v22_channels(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    all_rows = list(rows)
    a_pool = [row for row in all_rows if row["a_shape_pass"] and row["liquidity_pass"] and row["listing_pass"] and row["common_quality_pass"]]
    for key in ("ret1450", "activity", "amount1450", "range_expansion"):
        _percentiles(a_pool, key)
    for row in a_pool:
        row["channel"] = "A"
        row["rank_price"] = row["rank_ret1450"]
        row["rank_volatility"] = row["rank_range_expansion"]
        row["volume_score"] = (row["rank_activity"] + row["rank_amount1450"]) / 2.0
        row["score_A"] = (row["rank_price"] + row["volume_score"] + row["rank_volatility"]) / 3.0
    a_selected, a_ties = _select_with_boundary_ties(a_pool, V22_A_LIMIT, _v22_sort_a)
    a_codes = {row["sec_code"] for row in a_pool}
    b_pool = [row for row in all_rows if row["sec_code"] not in a_codes and row["b_shape_pass"] and row["liquidity_pass"] and row["listing_pass"] and row["common_quality_pass"]]
    for key, higher in (("impulse_return", True), ("impulse_amount_ratio", True), ("pullback_volume_ratio", False), ("position1450", True)):
        _percentiles(b_pool, key, higher_is_better=higher)
    for row in b_pool:
        row["channel"] = "B"
        row["score_B"] = sum(row[f"rank_{key}"] for key in ("impulse_return", "impulse_amount_ratio", "pullback_volume_ratio", "position1450")) / 4.0
    b_selected, b_ties = _select_with_boundary_ties(b_pool, V22_B_LIMIT, _v22_sort_b)
    selected_codes = {row["sec_code"] for row in a_selected + b_selected}
    for row in all_rows:
        row["selected"] = row["sec_code"] in selected_codes
        row["selected_top"] = row["selected"]
        row.setdefault("channel", None)
    return {"a_pool": a_pool, "b_pool": b_pool, "a_selected": a_selected, "b_selected": b_selected, "a_boundary_tie_expanded": a_ties, "b_boundary_tie_expanded": b_ties}


def v22_exit_decision(channel: str, code: str, previous: Mapping[str, Any] | None, current: Mapping[str, Any] | None) -> dict[str, Any]:
    """One post-D trading day; unavailable fields deliberately continue holding."""
    if not _history_ready(previous) or not current or current.get("status") != "ready":
        return {"status": "continue_invalid_previous_or_day"}
    key = "a_exit" if channel == "A" else "b_exit"
    detail = current.get(key, {})
    if detail.get("decision_status") != "ready" or not _finite(detail.get("decision_close")):
        return {"status": "continue_invalid_decision", "exit_window": detail.get("sell_window")}
    sell = detail.get("sell", {})
    if sell.get("status") != "ready" or not _finite(sell.get("vwap")):
        return {"status": "continue_unavailable_sell", "exit_window": detail.get("sell_window")}
    decision_close = float(detail["decision_close"])
    previous_close = float(previous["history"]["close"])
    if channel == "A":
        limit = _standard_limit_evidence(code, previous_close, decision_close)
        if limit["tail_limit_touch"]:
            return {"status": "continue_limit_up", "decision_close": decision_close, **limit}
    else:
        day_return = decision_close / previous_close - 1.0
        if day_return >= .03:
            return {"status": "continue_return_ge_3pct", "decision_close": decision_close, "day_return_1100": day_return}
    return {"status": "exit", "exit_price": float(sell["vwap"]), "exit_window": detail["sell_window"], "decision_close": decision_close, "decision_window": detail["decision_window"]}


def v23_exit_decision(channel: str, code: str, previous: Mapping[str, Any] | None, current: Mapping[str, Any] | None) -> dict[str, Any]:
    """V2.3's causal 13:04 decision and 13:05--13:10 executable exit."""
    if not _history_ready(previous) or not current or current.get("status") != "ready":
        return {"status": "continue_invalid_previous_or_day"}
    detail = current.get("v23_exit", {})
    if detail.get("decision_status") != "ready" or not _finite(detail.get("decision_close")):
        return {"status": "continue_invalid_decision", "exit_window": detail.get("sell_window")}
    sell = detail.get("sell", {})
    if sell.get("status") != "ready" or not _finite(sell.get("vwap")):
        return {"status": "continue_unavailable_sell", "exit_window": detail.get("sell_window")}
    decision_close = float(detail["decision_close"])
    previous_close = float(previous["history"]["close"])
    if channel == "A":
        limit = _standard_limit_evidence(code, previous_close, decision_close)
        if limit["tail_limit_touch"]:
            return {"status": "continue_limit_up", "decision_close": decision_close, **limit}
    else:
        day_return = decision_close / previous_close - 1.0
        if day_return >= .03:
            return {"status": "continue_return_ge_3pct", "decision_close": decision_close, "day_return_1304": day_return}
    return {"status": "exit", "exit_price": float(sell["vwap"]), "exit_window": detail["sell_window"], "decision_close": decision_close, "decision_window": detail["decision_window"]}


def v22_sleeve_step(sleeve: Mapping[str, Any], event: Mapping[str, Any] | None) -> dict[str, Any]:
    """Pure fixed-rank sleeve transition: cash is never borrowed or reallocated."""
    state = dict(sleeve)
    if state.get("holding") and event and event.get("exit_price"):
        state.update({"cash": float(state["cash"]) * (1.0 + float(event["net_return"])), "holding": False, "skipped": int(state.get("skipped", 0))})
    elif not state.get("holding") and event and event.get("buy_price"):
        state["holding"] = True
    elif state.get("holding") and event and event.get("buy_price"):
        state["skipped"] = int(state.get("skipped", 0)) + 1
    return state


def _v22_calendar(minute_root: str | Path) -> tuple[list[str], dict[str, int]]:
    first, last = date(2024, 8, 1), date(2024, 12, 31)
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
    positions = {value: index for index, value in enumerate(sessions)}
    if any(target not in positions or positions[target] < 10 for target in V22_MARKET_TARGETS):
        raise TailDataError("v22_canary_calendar_missing_dependency")
    return sessions, positions


def _v22_development_calendar(minute_root: str | Path) -> tuple[list[str], dict[str, int]]:
    """Discover only the frozen 2023--2024 minute sessions; never probe 2025."""
    sessions: list[str] = []
    cursor = date(2023, 1, 1)
    while cursor <= date(2024, 12, 31):
        if cursor.weekday() < 5:
            trade_date = _day_key(cursor)
            try:
                locate_day_source(minute_root, trade_date)
                sessions.append(trade_date)
            except TailDataError as exc:
                if exc.reason != f"minute_day_missing:{trade_date}":
                    raise
        cursor += timedelta(days=1)
    if len(sessions) < 12:
        raise TailDataError("v22_development_calendar_missing_dependency")
    return sessions, {value: index for index, value in enumerate(sessions)}


def _v22_final_row(
    code: str, target: str, index: int, day: Mapping[str, Any] | None, prior: list[Mapping[str, Any] | None],
    daily_root: str | Path, listing_cache: dict[str, dict[str, Any]], daily_consumed: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    provisional = {"listing_history_count_status": "at_least_threshold", "listing_history_sessions": MIN_HISTORY_SESSIONS, "listing_age_source": "deferred_until_shape_and_liquidity_pass"}
    row = build_v22_signal_row(code, day, prior, provisional)
    if row["common_quality_pass"] and row["shape_pass"] and row["liquidity_pass"]:
        listing = development_listing_evidence(daily_root, code, target, minute_visible_sessions=min(index, MIN_HISTORY_SESSIONS), cache=listing_cache)
        workbook = Path(daily_root) / f"{canonical_code(code)}.xlsx"
        if daily_consumed is not None and workbook.exists():
            daily_consumed[canonical_code(code)] = {"path": str(workbook), "sha256": _sha256(workbook)}
        row = build_v22_signal_row(code, day, prior, listing)
    else:
        row.update({"listing_pass": False, "listing_age_source": "not_evaluated_shape_or_liquidity_failed", "listing_history_sessions": None, "listing_history_count_status": "not_evaluated", "eligible_pass": False})
    row["trade_date"] = target
    return row


def _v23_final_row(
    code: str, target: str, index: int, day: Mapping[str, Any] | None, prior: list[Mapping[str, Any] | None],
    calendar: list[str], daily_root: str | Path, listing_cache: dict[str, dict[str, Any]], daily_consumed: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    provisional = {"listing_history_count_status": "at_least_threshold", "listing_history_sessions": MIN_HISTORY_SESSIONS, "listing_age_source": "deferred_until_shape_and_liquidity_pass"}
    row = build_v23_signal_row(code, day, prior, provisional)
    if row["common_quality_pass"] and row["shape_pass"] and row["liquidity_pass"]:
        listing = development_listing_evidence(daily_root, code, target, minute_visible_sessions=min(index, MIN_HISTORY_SESSIONS), cache=listing_cache)
        workbook = Path(daily_root) / f"{canonical_code(code)}.xlsx"
        if daily_consumed is not None and workbook.exists():
            daily_consumed[canonical_code(code)] = {"path": str(workbook), "sha256": _sha256(workbook)}
        row = build_v23_signal_row(code, day, prior, listing)
    else:
        row.update({"listing_pass": False, "listing_age_source": "not_evaluated_shape_or_liquidity_failed", "listing_history_sessions": None, "listing_history_count_status": "not_evaluated", "eligible_pass": False})
    row["trade_date"] = target
    row["prior5_amount_by_trade_date"] = [
        {"trade_date": calendar[index - 5 + offset], "full_amount": amount}
        for offset, amount in enumerate(row["prior5_amounts"])
    ]
    return row


def _v22_preselection(code: str, day: Mapping[str, Any] | None, d1: Mapping[str, Any] | None) -> dict[str, Any]:
    """Only necessary local conditions; B deliberately has no activity/close gate."""
    result = necessary_preselection(code, day, d1)
    if not result["quality_pass"]:
        return result
    result["b_local_necessary"] = bool(result["position1450"] >= POSITION1450_MIN)
    result["survives"] = bool(result["liquidity_pass"] and (result["a_local_necessary"] or result["b_local_necessary"]))
    result["reason"] = None if result["survives"] else "local_necessary_condition_failed"
    return result


def _v23_preselection(code: str, day: Mapping[str, Any] | None, d1: Mapping[str, Any] | None) -> dict[str, Any]:
    """D/D-1 remains a necessary-only prefilter; D-5..D-2 is checked later exactly."""
    return _v22_preselection(code, day, d1)


def _v22_input_manifest(minute_root: str | Path, daily_root: str | Path, calendar: Iterable[str]) -> dict[str, Any]:
    """Freeze allowed 2024 containers and the daily-K tree before a run exists."""
    containers = []
    for trade_date in sorted(set(calendar)):
        if not str(trade_date).startswith("2024"):
            raise TailDataError(f"v22_canary_year_guard:{trade_date}")
        source, kind = locate_day_source(minute_root, trade_date)
        identity = _zip_input_identity(source) if kind == "zip" else _directory_input_identity(source)
        containers.append({"trade_date": trade_date, "source_kind": kind, **identity})
    return {"minute_containers": containers, "daily_k_root": _daily_k_input_identity(daily_root)}


def _v22_development_input_manifest(minute_root: str | Path, daily_root: str | Path, calendar: Iterable[str]) -> dict[str, Any]:
    """Freeze every permitted development source, rejecting any other year."""
    containers = []
    for trade_date in sorted(set(calendar)):
        if not str(trade_date).startswith(("2023", "2024")):
            raise TailDataError(f"v22_development_year_guard:{trade_date}")
        source, kind = locate_day_source(minute_root, trade_date)
        identity = _zip_input_identity(source) if kind == "zip" else _directory_input_identity(source)
        containers.append({"trade_date": trade_date, "source_kind": kind, **identity})
    return {"minute_containers": containers, "daily_k_root": _daily_k_input_identity(daily_root)}


def _v22_prepare_run(
    output_dir: str | Path, manifest: Mapping[str, Any], *, resume_run_id: str | None,
    mode: str = "tnm-v22-1-canary", run_prefix: str = "v22-canary", spec_hash: str | None = None,
) -> tuple[Path, dict[str, Any]]:
    frozen_spec_hash = _spec_hash() if spec_hash is None else spec_hash
    identity = {
        "mode": mode, "spec_hash": frozen_spec_hash, "base_commit": _current_commit(),
        "source_blob_identity": _code_identity(), "input_manifest_hash": _json_identity(manifest),
    }
    run_hash = _json_identity(identity)
    run_id = f"{run_prefix}-{identity['spec_hash'][:12]}-{run_hash[:12]}"
    if resume_run_id is not None and resume_run_id != run_id:
        raise TailDataError("v22_resume_identity_mismatch")
    run_dir = Path(output_dir) / run_id
    record = {**identity, "run_hash": run_hash, "run_id": run_id, "input_manifest": manifest}
    path = run_dir / "run_manifest.json"
    if path.exists():
        if resume_run_id is None:
            raise TailDataError("v22_output_exists_requires_explicit_resume")
        if json.loads(path.read_text(encoding="utf-8")) != record:
            raise TailDataError("v22_resume_hash_mismatch")
    elif resume_run_id is not None:
        raise TailDataError("v22_resume_run_manifest_missing")
    else:
        _write_json(path, record)
    return run_dir, record


def _v22_controls(pool: Iterable[Mapping[str, Any]], selected: Iterable[Mapping[str, Any]], channel: str) -> dict[str, list[dict[str, Any]]]:
    size = len(list(selected))
    rows = [dict(row) for row in pool]
    key = lambda row: canonical_code(str(row["sec_code"]))
    return {
        "strategy": [dict(row) for row in selected],
        "random_same_n": sorted(rows, key=lambda row: (hashlib.sha256(f"20260826|V22|{row['trade_date']}|{channel}|{key(row)}".encode()).hexdigest(), key(row)))[:size],
        "ret1450_same_n": sorted(rows, key=lambda row: (-float(row["ret1450"]), key(row)))[:size],
        "activity_same_n": sorted(rows, key=lambda row: (-float(row["activity"]), key(row)))[:size],
    }


def _v22_slot_weights(selected: Iterable[Mapping[str, Any]], channel: str) -> list[float]:
    """Unique ranks occupy one slot; only the expanded boundary group shares one."""
    members = list(selected)
    limit = V22_A_LIMIT if channel == "A" else V22_B_LIMIT
    if len(members) <= limit:
        return [1.0] * len(members)
    boundary = [row for row in members if int(row.get("pool_rank", 0)) >= limit]
    boundary_codes = {str(row["sec_code"]) for row in boundary}
    return [1.0 / len(boundary) if str(row["sec_code"]) in boundary_codes else 1.0 for row in members]


def _v22_seed_event(row: Mapping[str, Any], *, group: str, rank: int, start_index: int, buy: Mapping[str, Any], signal_stat: Mapping[str, Any] | None, slot_weight: float) -> dict[str, Any]:
    ready = buy.get("status") == "ready" and _finite(buy.get("vwap"))
    return {
        "trade_date": str(row["trade_date"]), "sec_code": canonical_code(str(row["sec_code"])), "channel": str(row["channel"]),
        "strategy_or_control": group, "rank": rank, "start_index": start_index,
        "slot_weight": slot_weight,
        "selected": True, "bought": bool(ready), "outcome_status": "open" if ready else "unavailable_buy",
        "buy_price": float(buy["vwap"]) if ready else None, "exit_price": None, "gross_return": None, "net_return": None,
        "holding_days": None, "exit_trade_date": None, "exit_window": None, "decisions": [],
        "mark_price_by_date": {str(row["trade_date"]): float(signal_stat["history"]["close"])} if _history_ready(signal_stat) else {},
    }


def _v22_resolve_events(events: list[dict[str, Any]], calendar: list[str], load: Any, on_frontier: Any, consumed_codes_by_date: dict[str, set[str]]) -> None:
    """Resolve every open event date-by-date so a future container is opened once."""
    first = min((int(event["start_index"]) + 1 for event in events if event["outcome_status"] == "open"), default=len(calendar))
    for index in range(first, len(calendar)):
        open_events = [event for event in events if event["outcome_status"] == "open" and int(event["start_index"]) < index]
        if not open_events:
            continue
        codes = {str(event["sec_code"]) for event in open_events}
        consumed_codes_by_date.setdefault(calendar[index], set()).update(codes)
        consumed_codes_by_date.setdefault(calendar[index - 1], set()).update(codes)
        current = load(calendar[index], codes)
        previous = load(calendar[index - 1], codes)
        _v22_apply_exit_day(events, calendar, index, current, previous)
        on_frontier(calendar[index], len([event for event in events if event["outcome_status"] == "open"]))
    for event in events:
        if event["outcome_status"] == "open":
            event.update({"outcome_status": "unresolved_at_development_end", "holding_days": len(calendar) - int(event["start_index"]) - 1})


def _v22_apply_exit_day(
    events: Iterable[dict[str, Any]], calendar: list[str], index: int,
    current: Mapping[str, Mapping[str, Any]], previous: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Apply the approved V22 morning exit state machine to one trading day."""
    open_events = [event for event in events if event["outcome_status"] == "open" and int(event["start_index"]) < index]
    for event in open_events:
        code = str(event["sec_code"])
        decision = v22_exit_decision(event["channel"], code, previous.get(code), current.get(code))
        event["decisions"].append({"trade_date": calendar[index], **decision})
        current_stat = current.get(code, {})
        if _history_ready(current_stat):
            event["mark_price_by_date"][calendar[index]] = float(current_stat["history"]["close"])
        if decision["status"] == "exit":
            gross = float(decision["exit_price"]) / float(event["buy_price"]) - 1.0
            event.update({"outcome_status": "resolved", "exit_price": float(decision["exit_price"]), "gross_return": gross, "net_return": gross - .003, "holding_days": index - int(event["start_index"]), "exit_trade_date": calendar[index], "exit_window": decision["exit_window"]})
    return open_events


def _v23_apply_exit_day(
    events: Iterable[dict[str, Any]], calendar: list[str], index: int,
    current: Mapping[str, Mapping[str, Any]], previous: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Event research remains independent of the V2.3 account risk pause."""
    open_events = [event for event in events if event["outcome_status"] == "open" and int(event["start_index"]) < index]
    for event in open_events:
        code = str(event["sec_code"])
        current_stat = current.get(code, {})
        detail = current_stat.get("v23_exit", {})
        if detail.get("decision_status") == "ready" and _finite(detail.get("decision_close")):
            event.setdefault("mark_1304_by_date", {})[calendar[index]] = float(detail["decision_close"])
        decision = v23_exit_decision(event["channel"], code, previous.get(code), current_stat)
        event["decisions"].append({"trade_date": calendar[index], **decision})
        if decision["status"] == "exit":
            gross = float(decision["exit_price"]) / float(event["buy_price"]) - 1.0
            event.update({"outcome_status": "resolved", "exit_price": float(decision["exit_price"]), "gross_return": gross, "net_return": gross - .003, "holding_days": index - int(event["start_index"]), "exit_trade_date": calendar[index], "exit_window": decision["exit_window"]})
    return open_events


def _v23_resolve_events(events: list[dict[str, Any]], calendar: list[str], load: Any, on_frontier: Any, consumed_codes_by_date: dict[str, set[str]]) -> None:
    first = min((int(event["start_index"]) + 1 for event in events if event["outcome_status"] == "open"), default=len(calendar))
    for index in range(first, len(calendar)):
        open_events = [event for event in events if event["outcome_status"] == "open" and int(event["start_index"]) < index]
        if not open_events:
            continue
        codes = {str(event["sec_code"]) for event in open_events}
        consumed_codes_by_date.setdefault(calendar[index], set()).update(codes)
        consumed_codes_by_date.setdefault(calendar[index - 1], set()).update(codes)
        _v23_apply_exit_day(events, calendar, index, load(calendar[index], codes), load(calendar[index - 1], codes))
        on_frontier(calendar[index], len([event for event in events if event["outcome_status"] == "open"]))
    for event in events:
        if event["outcome_status"] == "open":
            event.update({"outcome_status": "unresolved_at_development_end", "holding_days": len(calendar) - int(event["start_index"]) - 1})


def v23_risk_snapshot(holdings: Mapping[str, Mapping[str, Any]], current: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """Mark every actual holding at 13:04, failing closed if any mark is absent."""
    marks, missing = [], []
    for sleeve, holding in sorted(holdings.items()):
        code = str(holding["event"]["sec_code"])
        detail = current.get(code, {}).get("v23_exit", {})
        if detail.get("decision_status") == "ready" and _finite(detail.get("decision_close")):
            mark, stale = float(detail["decision_close"]), False
        elif _finite(holding.get("last_mark")):
            mark, stale = float(holding["last_mark"]), True
        else:
            missing.append(sleeve)
            continue
        net_return = Decimal(str(mark)) / Decimal(str(holding["buy_price"])) - Decimal("1") - Decimal("0.003")
        marks.append({"sleeve": sleeve, "sec_code": code, "mark_1304": mark, "stale": stale, "net_return": float(net_return)})
    if missing:
        return {"status": "risk_snapshot_unavailable", "holding_count": len(holdings), "missing_sleeves": missing, "marks": marks, "risk_trigger": False}
    returns = [Decimal(str(value["net_return"])) for value in marks]
    all_losing = len(returns) >= 3 and all(value < 0 for value in returns)
    mean_decimal = sum(returns) / len(returns) if returns else None
    mean_return = float(mean_decimal) if mean_decimal is not None else None
    mean_loss_over_5pct = bool(mean_decimal is not None and mean_decimal < Decimal("-0.05"))
    return {"status": "ready", "holding_count": len(returns), "marks": marks, "all_losing": all_losing, "mean_holding_return": mean_return, "mean_loss_over_5pct": mean_loss_over_5pct, "risk_trigger": bool(all_losing or mean_loss_over_5pct), "stale_mark_count": sum(value["stale"] for value in marks)}


def _v23_exit_from_account(sleeve: str, state: dict[str, Any], current: Mapping[str, Mapping[str, Any]], trade_date: str, ledger: list[dict[str, Any]], *, forced: bool) -> bool:
    holding = state.get("holding")
    if holding is None:
        return False
    detail = current.get(str(holding["event"]["sec_code"]), {}).get("v23_exit", {})
    sell = detail.get("sell", {})
    if sell.get("status") != "ready" or not _finite(sell.get("vwap")):
        holding["force_exit_pending"] = True
        ledger.append({"trade_date": trade_date, "sleeve": sleeve, "action": "forced_exit_unavailable" if forced else "continue_unavailable_sell", "sec_code": holding["event"]["sec_code"], "exit_window": detail.get("sell_window")})
        return False
    exit_price = float(sell["vwap"])
    net_return = exit_price / float(holding["buy_price"]) - 1.0 - .003
    state["cash"] *= 1.0 + net_return
    state["holding"] = None
    ledger.append({"trade_date": trade_date, "sleeve": sleeve, "action": "forced_risk_exit" if forced else "sell", "sec_code": holding["event"]["sec_code"], "buy_price": holding["buy_price"], "exit_price": exit_price, "net_return": net_return, "exit_window": detail.get("sell_window")})
    return True


def _v23_sleeve_account(events: Iterable[Mapping[str, Any]], calendar: Iterable[str], load: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Actual nine-sleeve account; event economics are deliberately not mutated."""
    dates = list(calendar)
    initial = 5_000_000.0 / 9.0
    sleeves = {f"A{rank}": {"cash": initial, "holding": None} for rank in range(1, V22_A_LIMIT + 1)} | {f"B{rank}": {"cash": initial, "holding": None} for rank in range(1, V22_B_LIMIT + 1)}
    strategy = [dict(event) for event in events if event["strategy_or_control"] == "strategy"]
    boundary = [event for event in strategy if int(event["rank"]) > (V22_A_LIMIT if event["channel"] == "A" else V22_B_LIMIT)]
    if boundary:
        return [], [], {"status": "blocked_boundary_tie_account", "boundary_tie_members": boundary}, [], [], []
    by_signal: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in strategy:
        by_signal[str(event["trade_date"])].append(event)
    ledger: list[dict[str, Any]] = []
    nav: list[dict[str, Any]] = []
    holdings_rows: list[dict[str, Any]] = []
    risk_rows: list[dict[str, Any]] = []
    cooldown_skips: list[dict[str, Any]] = []
    episodes: list[dict[str, Any]] = []
    cooldown_dates: set[int] = set()
    risk_episode = False
    trigger_counts = {"all_losing": 0, "mean_loss_over_5pct": 0, "both": 0}
    stale_marks = busy_skip = forced_unavailable = 0
    realized: list[float] = []
    for index, trade_date in enumerate(dates):
        active = {name: sleeve["holding"] for name, sleeve in sleeves.items() if sleeve["holding"] is not None}
        had_holdings_at_start = bool(active)
        codes = {str(holding["event"]["sec_code"]) for holding in active.values()}
        current = load(trade_date, codes) if codes else {}
        previous = load(dates[index - 1], codes) if codes and index else {}
        snapshot = v23_risk_snapshot(active, current) if active else {"status": "no_holdings", "holding_count": 0, "marks": [], "risk_trigger": False}
        for mark in snapshot.get("marks", []):
            holding = sleeves[mark["sleeve"]]["holding"]
            if holding is not None and not mark["stale"]:
                holding["last_mark"] = float(mark["mark_1304"])
                holding["last_mark_date"] = trade_date
            stale_marks += int(mark["stale"])
            holdings_rows.append({"trade_date": trade_date, **mark})
        trigger = bool(snapshot.get("risk_trigger")) and not risk_episode
        if trigger:
            risk_episode = True
            cooldown_dates = set(range(index + 1, min(index + 4, len(dates))))
            trigger_counts["all_losing"] += int(bool(snapshot["all_losing"]))
            trigger_counts["mean_loss_over_5pct"] += int(bool(snapshot["mean_loss_over_5pct"]))
            trigger_counts["both"] += int(bool(snapshot["all_losing"] and snapshot["mean_loss_over_5pct"]))
            episodes.append({"trigger_trade_date": trade_date, "holding_count": snapshot["holding_count"], "marks": snapshot["marks"], "mean_holding_return": snapshot["mean_holding_return"], "stale_mark_count": snapshot["stale_mark_count"], "earliest_restore_trade_date": dates[index + 4] if index + 4 < len(dates) else None, "planned_forced_exits": len(active), "actual_forced_exits": 0})
            for name in sorted(active):
                if _v23_exit_from_account(name, sleeves[name], current, trade_date, ledger, forced=True):
                    episodes[-1]["actual_forced_exits"] += 1
                    realized.append(float(ledger[-1]["net_return"]))
                else:
                    forced_unavailable += 1
        else:
            for name in sorted(active):
                holding = sleeves[name]["holding"]
                if holding is None:
                    continue
                if holding.get("force_exit_pending"):
                    if _v23_exit_from_account(name, sleeves[name], current, trade_date, ledger, forced=True):
                        realized.append(float(ledger[-1]["net_return"]))
                    else:
                        forced_unavailable += 1
                    continue
                decision = v23_exit_decision(holding["event"]["channel"], holding["event"]["sec_code"], previous.get(holding["event"]["sec_code"]), current.get(holding["event"]["sec_code"]))
                if decision["status"] == "exit":
                    exit_price = float(decision["exit_price"])
                    net_return = exit_price / float(holding["buy_price"]) - 1.0 - .003
                    sleeves[name]["cash"] *= 1.0 + net_return
                    sleeves[name]["holding"] = None
                    ledger.append({"trade_date": trade_date, "sleeve": name, "action": "sell", "sec_code": holding["event"]["sec_code"], "buy_price": holding["buy_price"], "exit_price": exit_price, "net_return": net_return, "exit_window": decision["exit_window"]})
                    realized.append(net_return)
        if risk_episode and index not in cooldown_dates and not trigger and not had_holdings_at_start and not any(sleeve["holding"] is not None for sleeve in sleeves.values()):
            risk_episode = False
            if episodes and episodes[-1].get("actual_restore_trade_date") is None:
                episodes[-1]["actual_restore_trade_date"] = trade_date
                earliest = episodes[-1].get("earliest_restore_trade_date")
                episodes[-1]["delayed_clear_days"] = max(0, index - dates.index(earliest)) if earliest else None
        for event in sorted(by_signal.get(trade_date, []), key=lambda value: (value["channel"], value["rank"])):
            sleeve_name, sleeve = f"{event['channel']}{event['rank']}", sleeves[f"{event['channel']}{event['rank']}"]
            if trigger:
                ledger.append({"trade_date": trade_date, "sleeve": sleeve_name, "action": "risk_trigger_no_buy", "sec_code": event["sec_code"]})
            elif index in cooldown_dates:
                record = {"trade_date": trade_date, "sleeve": sleeve_name, "sec_code": event["sec_code"], "action": "cooldown_skip", "hypothetical_net_return": event.get("net_return")}
                cooldown_skips.append(record)
                ledger.append(record)
            elif risk_episode:
                ledger.append({"trade_date": trade_date, "sleeve": sleeve_name, "action": "risk_off_waiting_for_clear", "sec_code": event["sec_code"]})
            elif sleeve["holding"] is not None:
                busy_skip += 1
                ledger.append({"trade_date": trade_date, "sleeve": sleeve_name, "action": "busy_skip", "sec_code": event["sec_code"]})
            elif not event["bought"]:
                ledger.append({"trade_date": trade_date, "sleeve": sleeve_name, "action": "unavailable_buy", "sec_code": event["sec_code"]})
            else:
                sleeve["holding"] = {"event": event, "buy_price": float(event["buy_price"]), "last_mark": float(event["buy_price"]), "last_mark_date": trade_date, "force_exit_pending": False}
                ledger.append({"trade_date": trade_date, "sleeve": sleeve_name, "action": "buy", "sec_code": event["sec_code"], "buy_price": event["buy_price"]})
        value = 0.0
        active_after = 0
        for sleeve in sleeves.values():
            holding = sleeve["holding"]
            if holding is None:
                value += float(sleeve["cash"])
            else:
                active_after += 1
                value += float(sleeve["cash"]) * float(holding["last_mark"]) / float(holding["buy_price"])
        risk_rows.append({"trade_date": trade_date, "state": "RISK_OFF" if risk_episode or trigger else "NORMAL", "triggered": trigger, **snapshot})
        nav.append({"trade_date": trade_date, "nav": value, "utilization": active_after / 9.0})
    peak, drawdown = 0.0, 0.0
    for row in nav:
        peak = max(peak, float(row["nav"]))
        drawdown = min(drawdown, float(row["nav"]) / peak - 1.0)
    skipped = [value.get("hypothetical_net_return") for value in cooldown_skips if _finite(value.get("hypothetical_net_return"))]
    longest_loss = current_loss = 0
    for value in realized:
        current_loss = current_loss + 1 if value < 0 else 0
        longest_loss = max(longest_loss, current_loss)
    unresolved = any(sleeve["holding"] is not None for sleeve in sleeves.values())
    annual_returns: dict[str, float] = {}
    monthly_returns: dict[str, float] = {}
    for index in range(1, len(nav)):
        daily_return = float(nav[index]["nav"]) / float(nav[index - 1]["nav"]) - 1.0
        for bucket, key in ((annual_returns, str(nav[index]["trade_date"])[:4]), (monthly_returns, str(nav[index]["trade_date"])[:6])):
            bucket[key] = (1.0 + bucket.get(key, 0.0)) * (1.0 + daily_return) - 1.0
    account = {
        "status": "blocked_unresolved_account" if unresolved else "ready", "busy_skip": busy_skip,
        "total_return": None if unresolved else nav[-1]["nav"] / 5_000_000.0 - 1.0,
        "diagnostic_mtm_nav": nav[-1]["nav"] if nav else 5_000_000.0, "max_drawdown": drawdown,
        "average_utilization": sum(float(row["utilization"]) for row in nav) / len(nav) if nav else 0.0,
        "realized_trade_count": len(realized), "realized_win_rate": sum(value > 0 for value in realized) / len(realized) if realized else None,
        "realized_net_mean": sum(realized) / len(realized) if realized else None, "realized_net_median": float(pd.Series(realized).median()) if realized else None,
        "longest_consecutive_loss": longest_loss, "risk_trigger_count": len(episodes), "risk_trigger_all_losing_count": trigger_counts["all_losing"],
        "risk_trigger_mean_loss_over_5pct_count": trigger_counts["mean_loss_over_5pct"], "risk_trigger_both_count": trigger_counts["both"],
        "forced_exit_unavailable_count": forced_unavailable, "stale_mark_count": stale_marks, "cooldown_skip_count": len(cooldown_skips),
        "cooldown_avoided_hypothetical_loss": sum(-float(value) for value in skipped if value < 0),
        "cooldown_missed_hypothetical_profit": sum(float(value) for value in skipped if value > 0), "risk_episodes": episodes,
        "annual_returns": annual_returns, "monthly_returns": monthly_returns,
    }
    return ledger, nav, account, holdings_rows, risk_rows, cooldown_skips


def _v22_group_metrics(values: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    rows = [dict(value) for value in values]
    resolved_rows = [value for value in rows if value.get("outcome_status") == "resolved"]
    resolved = [float(value["net_return"]) for value in resolved_rows]
    gross = [float(value["gross_return"]) for value in resolved_rows]
    holding = [float(value["holding_days"]) for value in resolved_rows if value.get("holding_days") is not None]
    dates = sorted({value["trade_date"] for value in rows})
    unresolved = sum(value.get("outcome_status") == "unresolved_at_development_end" for value in rows)
    return {
        "selected": len(rows), "bought": sum(bool(value.get("bought")) for value in rows), "resolved": len(resolved), "unresolved": unresolved,
        "unavailable_buy": sum(value.get("outcome_status") == "unavailable_buy" for value in rows),
        "coverage": len(resolved) / len(rows) if rows else None, "gross_win_rate": sum(value > 0 for value in gross) / len(gross) if gross else None, "net_win_rate": sum(value > 0 for value in resolved) / len(resolved) if resolved else None,
        "gross": _v22_distribution(gross), "net": _v22_distribution(resolved), "holding_sessions": _v22_distribution(holding),
        "sum_event_net_return": sum(resolved) if resolved else 0.0, "distinct_signal_dates": len(dates),
        "sample_status": "ready" if len(rows) >= 100 and len(dates) >= 60 else "insufficient_sample",
        "economic_status": "blocked_data_quality" if rows and len(resolved) / len(rows) < .99 else "blocked_unresolved" if unresolved else "ready",
    }


def _v22_distribution(values: Iterable[float]) -> dict[str, float | None]:
    series = pd.Series(list(values), dtype=float)
    if series.empty:
        return {key: None for key in ("mean", "median", "std", "p10", "p25", "p75", "p90", "min", "max")}
    return {"mean": float(series.mean()), "median": float(series.median()), "std": float(series.std(ddof=0)), "p10": float(series.quantile(.10)), "p25": float(series.quantile(.25)), "p75": float(series.quantile(.75)), "p90": float(series.quantile(.90)), "min": float(series.min()), "max": float(series.max())}


def _v22_daily_slots(values: Iterable[Mapping[str, Any]], channel: str, group: str) -> list[dict[str, Any]]:
    slots = V22_A_LIMIT if channel == "A" else V22_B_LIMIT
    by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for value in values:
        by_day[str(value["trade_date"])].append(dict(value))
    result = []
    for trade_date, rows in sorted(by_day.items()):
        unresolved = any(row.get("outcome_status") == "unresolved_at_development_end" for row in rows)
        weighted = sum(float(row["net_return"]) * float(row.get("slot_weight", 1.0)) for row in rows if row.get("outcome_status") == "resolved")
        result.append({"trade_date": trade_date, "channel": channel, "strategy_or_control": group, "daily_slot_net": None if unresolved else weighted / slots, "nominal_slots": slots, "selected_slots": len(rows), "selected_slot_weight": sum(float(row.get("slot_weight", 1.0)) for row in rows), "status": "blocked_unresolved" if unresolved else "ready"})
    return result


def _v22_fixed_slot_summary(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    values = [float(row["daily_slot_net"]) for row in sorted(rows, key=lambda row: str(row["trade_date"])) if row.get("daily_slot_net") is not None]
    distribution = _v22_distribution(values)
    equity, peak, max_drawdown = 1.0, 1.0, 0.0
    for value in values:
        equity *= 1.0 + value
        peak = max(peak, equity)
        max_drawdown = min(max_drawdown, equity / peak - 1.0)
    return {**distribution, "arithmetic_sum": sum(values), "compound_diagnostic": equity - 1.0, "max_drawdown": max_drawdown, "observation_count": len(values)}


def _v22_month_block_bootstrap(daily_slots: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    ready = [dict(row) for row in daily_slots if row.get("daily_slot_net") is not None]
    by_month: dict[str, list[float]] = defaultdict(list)
    for row in ready:
        by_month[str(row["trade_date"])[:6]].append(float(row["daily_slot_net"]))
    months = sorted(by_month)
    if len(months) < 2:
        return {"status": "insufficient_for_bootstrap", "replications": 0, "mean_daily_slot_net_ci95": None, "daily_win_rate_ci95": None}
    samples, wins = [], []
    for replicate in range(256):
        chosen = [months[int(hashlib.sha256(f"V22-bootstrap|{replicate}|{slot}".encode()).hexdigest(), 16) % len(months)] for slot in range(len(months))]
        values = [value for month in chosen for value in by_month[month]]
        samples.append(sum(values) / len(values))
        wins.append(sum(value > 0 for value in values) / len(values))
    series = pd.Series(samples)
    win_series = pd.Series(wins)
    return {"status": "diagnostic", "replications": 256, "mean_daily_slot_net_ci95": [float(series.quantile(.025)), float(series.quantile(.975))], "daily_win_rate_ci95": [float(win_series.quantile(.025)), float(win_series.quantile(.975))]}


def _v22_relative_month_bootstrap(strategy: Iterable[Mapping[str, Any]], control: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    left = {str(row["trade_date"]): float(row["daily_slot_net"]) for row in strategy if row.get("daily_slot_net") is not None}
    right = {str(row["trade_date"]): float(row["daily_slot_net"]) for row in control if row.get("daily_slot_net") is not None}
    by_month: dict[str, list[float]] = defaultdict(list)
    for trade_date in sorted(set(left) & set(right)):
        by_month[trade_date[:6]].append(left[trade_date] - right[trade_date])
    months = sorted(by_month)
    if len(months) < 2:
        return {"status": "insufficient_for_bootstrap", "ci95": None}
    samples = []
    for replicate in range(256):
        picked = [months[int(hashlib.sha256(f"V22-relative|{replicate}|{slot}".encode()).hexdigest(), 16) % len(months)] for slot in range(len(months))]
        values = [value for month in picked for value in by_month[month]]
        samples.append(sum(values) / len(values))
    series = pd.Series(samples)
    return {"status": "diagnostic", "ci95": [float(series.quantile(.025)), float(series.quantile(.975))]}


def _v22_combined_channel_slots(slots: Iterable[Mapping[str, Any]], group: str) -> list[dict[str, Any]]:
    """Combine same-day A/B slots by their actual fixed-slot weights, not event counts."""
    by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in slots:
        if str(row["strategy_or_control"]) == group and str(row["channel"]) in {"A", "B"}:
            by_day[str(row["trade_date"])].append(dict(row))
    result: list[dict[str, Any]] = []
    for trade_date, members in sorted(by_day.items()):
        blocked = any(member.get("daily_slot_net") is None for member in members)
        actual_weight = sum(float(member.get("selected_slot_weight", 0.0)) for member in members)
        weighted_net = sum(float(member["daily_slot_net"]) * float(member["nominal_slots"]) for member in members if member.get("daily_slot_net") is not None)
        result.append({
            "trade_date": trade_date, "channel": "AB", "strategy_or_control": group,
            "daily_slot_net": None if blocked or actual_weight <= 0 else weighted_net / actual_weight,
            "nominal_slots": V22_A_LIMIT + V22_B_LIMIT, "selected_slots": sum(int(member.get("selected_slots", 0)) for member in members),
            "selected_slot_weight": actual_weight, "status": "blocked_unresolved" if blocked else "ready",
        })
    return result


def _v22_event_aggregates(events: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Shared canary/development consumer with year/month, slots and deterministic bootstrap."""
    rows = [dict(event) for event in events]
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in rows:
        groups[f"{event['channel']}|{event['strategy_or_control']}"] .append(event)
    overall = {name: _v22_group_metrics(values) for name, values in sorted(groups.items())}
    yearly = {f"{name}|{year}": _v22_group_metrics(value for value in values if str(value["trade_date"]).startswith(year)) for name, values in sorted(groups.items()) for year in sorted({str(value["trade_date"])[:4] for value in values})}
    monthly = {f"{name}|{month}": _v22_group_metrics(value for value in values if str(value["trade_date"]).startswith(month)) for name, values in sorted(groups.items()) for month in sorted({str(value["trade_date"])[:6] for value in values})}
    slots = [slot for name, values in groups.items() for slot in _v22_daily_slots(values, name.split("|", 1)[0], name.split("|", 1)[1])]
    slot_stats: dict[str, dict[str, Any]] = {}
    bootstrap: dict[str, dict[str, Any]] = {}
    for name in groups:
        scoped = [row for row in slots if f"{row['channel']}|{row['strategy_or_control']}" == name]
        slot_stats[f"{name}|combined"] = _v22_fixed_slot_summary(scoped)
        bootstrap[f"{name}|combined"] = _v22_month_block_bootstrap(scoped)
        for year in sorted({str(row["trade_date"])[:4] for row in scoped}):
            annual = [row for row in scoped if str(row["trade_date"]).startswith(year)]
            slot_stats[f"{name}|{year}"] = _v22_fixed_slot_summary(annual)
            bootstrap[f"{name}|{year}"] = _v22_month_block_bootstrap(annual)
    for channel in ("A", "B"):
        controls = [name for name in groups if name.startswith(f"{channel}|") and not name.endswith("|strategy")]
        strategy = f"{channel}|strategy"
        if strategy in groups and controls:
            best = max(controls, key=lambda name: (slot_stats[f"{name}|combined"]["mean"] if slot_stats[f"{name}|combined"]["mean"] is not None else -math.inf, name))
            strategy_slots = [row for row in slots if f"{row['channel']}|{row['strategy_or_control']}" == strategy]
            control_slots = [row for row in slots if f"{row['channel']}|{row['strategy_or_control']}" == best]
            bootstrap[f"{strategy}|combined"]["relative_best_control"] = {"control": best, **_v22_relative_month_bootstrap(strategy_slots, control_slots)}
    combined_groups = sorted({str(event["strategy_or_control"]) for event in rows})
    combined_channel: dict[str, Any] = {"overall": {}, "yearly": {}, "monthly": {}, "daily_slot_net": [], "daily_slot_statistics": {}, "bootstrap": {}}
    for group in combined_groups:
        group_rows = [event for event in rows if str(event["strategy_or_control"]) == group and str(event["channel"]) in {"A", "B"}]
        combined_channel["overall"][group] = _v22_group_metrics(group_rows)
        for year in sorted({str(event["trade_date"])[:4] for event in group_rows}):
            combined_channel["yearly"][f"{group}|{year}"] = _v22_group_metrics(event for event in group_rows if str(event["trade_date"]).startswith(year))
        for month in sorted({str(event["trade_date"])[:6] for event in group_rows}):
            combined_channel["monthly"][f"{group}|{month}"] = _v22_group_metrics(event for event in group_rows if str(event["trade_date"]).startswith(month))
        scoped = _v22_combined_channel_slots(slots, group)
        combined_channel["daily_slot_net"].extend(scoped)
        combined_channel["daily_slot_statistics"][f"{group}|combined"] = _v22_fixed_slot_summary(scoped)
        combined_channel["bootstrap"][f"{group}|combined"] = _v22_month_block_bootstrap(scoped)
        for year in sorted({str(row["trade_date"])[:4] for row in scoped}):
            annual = [row for row in scoped if str(row["trade_date"]).startswith(year)]
            combined_channel["daily_slot_statistics"][f"{group}|{year}"] = _v22_fixed_slot_summary(annual)
            combined_channel["bootstrap"][f"{group}|{year}"] = _v22_month_block_bootstrap(annual)
    controls = [group for group in combined_groups if group != "strategy"]
    if "strategy" in combined_groups and controls:
        best = max(controls, key=lambda group: (combined_channel["daily_slot_statistics"][f"{group}|combined"]["mean"] if combined_channel["daily_slot_statistics"][f"{group}|combined"]["mean"] is not None else -math.inf, group))
        strategy_slots = [row for row in combined_channel["daily_slot_net"] if row["strategy_or_control"] == "strategy"]
        control_slots = [row for row in combined_channel["daily_slot_net"] if row["strategy_or_control"] == best]
        combined_channel["bootstrap"]["strategy|combined"]["relative_best_control"] = {"control": best, **_v22_relative_month_bootstrap(strategy_slots, control_slots)}
    return {"overall": overall, "yearly": yearly, "monthly": monthly, "daily_slot_net": slots, "daily_slot_statistics": slot_stats, "bootstrap": bootstrap, "combined_channel": combined_channel}


def _v22_sleeve_account(events: Iterable[Mapping[str, Any]], calendar: Iterable[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Nine fixed sleeves driven by the same strategy events; exits are processed before buys."""
    initial = 5_000_000.0 / 9.0
    sleeves = {f"A{rank}": {"cash": initial, "event": None} for rank in range(1, 7)} | {f"B{rank}": {"cash": initial, "event": None} for rank in range(1, 4)}
    strategy = [dict(event) for event in events if event["strategy_or_control"] == "strategy"]
    boundary = [event for event in strategy if int(event["rank"]) > (V22_A_LIMIT if event["channel"] == "A" else V22_B_LIMIT)]
    if boundary:
        return [], [], {"status": "blocked_boundary_tie_account", "boundary_tie_members": [{"trade_date": event["trade_date"], "channel": event["channel"], "sec_code": event["sec_code"], "rank": event["rank"]} for event in boundary], "busy_skip": 0, "total_return": None, "diagnostic_mtm_nav": None, "stale_mark_days": 0}
    by_signal: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_exit: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in strategy:
        by_signal[event["trade_date"]].append(event)
        if event.get("exit_trade_date"):
            by_exit[str(event["exit_trade_date"])].append(event)
    ledger, nav = [], []
    busy_skip = 0
    stale_mark_days = 0
    for trade_date in calendar:
        for event in by_exit.get(str(trade_date), []):
            sleeve = sleeves[f"{event['channel']}{event['rank']}"]
            if sleeve["event"] is event:
                sleeve["cash"] *= 1.0 + float(event["net_return"])
                sleeve["event"] = None
                ledger.append({"trade_date": trade_date, "sleeve": f"{event['channel']}{event['rank']}", "action": "sell", "sec_code": event["sec_code"], "net_return": event["net_return"]})
        for event in sorted(by_signal.get(str(trade_date), []), key=lambda value: (value["channel"], value["rank"])):
            name, sleeve = f"{event['channel']}{event['rank']}", sleeves[f"{event['channel']}{event['rank']}"]
            if sleeve["event"] is not None:
                busy_skip += 1
                ledger.append({"trade_date": trade_date, "sleeve": name, "action": "busy_skip", "sec_code": event["sec_code"]})
            elif not event["bought"]:
                ledger.append({"trade_date": trade_date, "sleeve": name, "action": "unavailable_buy", "sec_code": event["sec_code"]})
            else:
                sleeve["event"] = event
                ledger.append({"trade_date": trade_date, "sleeve": name, "action": "buy", "sec_code": event["sec_code"], "buy_price": event["buy_price"]})
        value = 0.0
        for sleeve in sleeves.values():
            event = sleeve["event"]
            if event is None:
                value += float(sleeve["cash"])
                continue
            marks = {str(key): float(mark) for key, mark in event.get("mark_price_by_date", {}).items() if str(key) <= str(trade_date)}
            if str(trade_date) in marks:
                mark = marks[str(trade_date)]
            elif marks:
                mark = marks[max(marks)]
                stale_mark_days += 1
            else:
                mark = float(event["buy_price"])
                stale_mark_days += 1
            value += float(sleeve["cash"]) * mark / float(event["buy_price"])
        nav.append({"trade_date": trade_date, "nav": value, "utilization": sum(sleeve["event"] is not None for sleeve in sleeves.values()) / 9.0})
    unresolved = any(sleeve["event"] is not None for sleeve in sleeves.values())
    return ledger, nav, {"status": "blocked_unresolved_account" if unresolved else "ready", "busy_skip": busy_skip, "total_return": None if unresolved else nav[-1]["nav"] / 5_000_000.0 - 1.0, "diagnostic_mtm_nav": nav[-1]["nav"] if nav else 5_000_000.0, "stale_mark_days": stale_mark_days}


def _v22_checkpoint_identity(
    records: Mapping[str, Mapping[str, Any]], daily: Mapping[str, Mapping[str, str]], target: str, index: int,
    rows: Iterable[Mapping[str, Any]], events: Iterable[Mapping[str, Any]], calendar: list[str],
) -> dict[str, Any]:
    """Only members actually used by this target's selection and exit decisions."""
    dependencies: dict[str, set[str] | None] = {target: None, calendar[index - 1]: None}
    evaluated = {str(row["sec_code"]) for row in rows}
    for trade_date in calendar[index - 10:index - 1]:
        dependencies[trade_date] = set(evaluated)
    for event in events:
        code = str(event["sec_code"])
        for decision in event.get("decisions", []):
            trade_date = str(decision["trade_date"])
            if trade_date not in dependencies:
                dependencies[trade_date] = {code}
            elif dependencies[trade_date] is not None:
                dependencies[trade_date].add(code)
            previous_index = calendar.index(trade_date) - 1
            previous_date = calendar[previous_index]
            if previous_date not in dependencies:
                dependencies[previous_date] = {code}
            elif dependencies[previous_date] is not None:
                dependencies[previous_date].add(code)
    daily_subset = {code: evidence for code, evidence in daily.items() if code in evaluated}
    return _unit_consumed_input(_unit_minute_members(records, dependencies), daily_subset)


def _v22_verify_saved_units(run_dir: Path, run_hash: str, minute_root: str | Path) -> dict[str, Any]:
    saved: dict[str, Any] = {}
    source_records: dict[str, dict[str, Any]] = {}
    checkpoints = []
    required_members: dict[str, set[str]] = defaultdict(set)
    for path in sorted((run_dir / "checkpoints").glob("market_*.json")) if (run_dir / "checkpoints").exists() else []:
        checkpoint = json.loads(path.read_text(encoding="utf-8"))
        if checkpoint.get("run_hash") != run_hash:
            raise TailDataError("v22_checkpoint_hash_mismatch")
        result = checkpoint.get("result", {})
        checkpoints.append((checkpoint, result))
        for day, codes in result.get("unit_consumed_input", {}).get("minute_members", {}).items():
            required_members[str(day)].update(str(code) for code in codes)
    for day, codes in sorted(required_members.items()):
        _, source_records[day] = load_day_v2_statistics(minute_root, day, codes)
    for checkpoint, result in checkpoints:
        _verify_checkpoint_consumed_input(str(checkpoint["unit"]), result, source_records)
        saved[str(checkpoint["unit"])] = result
    return saved


def run_v22_canary(minute_root: str | Path, daily_root: str | Path, output_dir: str | Path, *, resume_run_id: str | None = None, max_elapsed_seconds: float = 900.0) -> tuple[dict[str, Any], Path]:
    """V2.2's production chain: shared source batches, controls, events and sleeves."""
    started, run_dir = time.monotonic(), None
    calendar, positions = _v22_calendar(minute_root)
    manifest_input = _v22_input_manifest(minute_root, daily_root, calendar)
    run_dir, manifest = _v22_prepare_run(output_dir, manifest_input, resume_run_id=resume_run_id)
    run_hash, progress_path, completion_path = manifest["run_hash"], run_dir / "progress.json", run_dir / "completion.json"
    preserve_existing_terminal = completion_path.exists()
    cache: dict[str, dict[str, dict[str, Any]]] = {}
    records: dict[str, dict[str, Any]] = {}
    listing_cache: dict[str, dict[str, Any]] = {}
    daily_consumed: dict[str, dict[str, str]] = {}
    completed_sources: list[str] = []

    def progress(stage: str, completed: Iterable[str], open_events: int = 0) -> None:
        _write_json(progress_path, {"run_hash": run_hash, "stage": stage, "completed_units": sorted(completed), "total_units": len(V22_MARKET_TARGETS), "source_frontier": completed_sources[-1] if completed_sources else None, "completed_source_dates": list(completed_sources), "open_exit_events": open_events, "elapsed_seconds": round(time.monotonic() - started, 6), "updated_at": datetime.now(timezone.utc).isoformat()})

    def time_limit() -> None:
        if time.monotonic() - started >= float(max_elapsed_seconds):
            raise TailDataError("canary_time_limit")

    def load(trade_date: str, codes: Iterable[str] | None) -> dict[str, dict[str, Any]]:
        requested = None if codes is None else {canonical_code(code) for code in codes}
        if trade_date in cache:
            if requested is not None and not requested.issubset(cache[trade_date]):
                raise TailDataError(f"v22_container_reopen_forbidden:{trade_date}")
            return cache[trade_date]
        values, record = load_day_v2_statistics(minute_root, trade_date, requested)
        cache[trade_date], records[trade_date] = values, record
        completed_sources.append(trade_date)
        progress(f"source:{trade_date}", ())
        time_limit()
        return values

    try:
        if completion_path.exists():
            completion = json.loads(completion_path.read_text(encoding="utf-8"))
            if completion.get("status") == "SUCCEEDED":
                if resume_run_id is None or completion.get("run_hash") != run_hash:
                    raise TailDataError("v22_succeeded_requires_explicit_matching_resume")
                _v22_verify_saved_units(run_dir, run_hash, minute_root)
                _verify_succeeded_artifacts(run_dir, completion)
                return json.loads((run_dir / "v22_summary.json").read_text(encoding="utf-8")), run_dir
        saved = _v22_verify_saved_units(run_dir, run_hash, minute_root) if resume_run_id else {}
        progress("input_frozen", saved)
        all_rows: dict[str, list[dict[str, Any]]] = {}
        ranked_by_target: dict[str, dict[str, Any]] = {}
        preselected: dict[str, tuple[int, dict[str, dict[str, Any]], set[str]]] = {}
        for target in V22_MARKET_TARGETS:
            unit, index = f"market:{target}", positions[target]
            if unit in saved:
                continue
            day, d1 = load(target, None), load(calendar[index - 1], None)
            pre = {code: _v22_preselection(code, day.get(code), d1.get(code)) for code in day}
            evaluation = {code for code, proof in pre.items() if proof["survives"]}
            preselected[target] = (index, day, evaluation)

        evaluation_union = set().union(*(evaluation for _, _, evaluation in preselected.values())) if preselected else set()
        shared_history_dates = {
            dependency
            for target, (index, _, _) in preselected.items()
            for dependency in calendar[index - 10:index - 1]
        }
        for dependency in sorted(shared_history_dates, key=calendar.index):
            load(dependency, evaluation_union)

        for target in V22_MARKET_TARGETS:
            unit = f"market:{target}"
            if unit in saved:
                continue
            index, day, evaluation = preselected[target]
            d1 = cache[calendar[index - 1]]
            rows = []
            for code in sorted(evaluation):
                prior = [cache[value].get(code) for value in calendar[index - 10:index - 1]] + [d1.get(code)]
                rows.append(_v22_final_row(code, target, index, day.get(code), prior, daily_root, listing_cache, daily_consumed))
            all_rows[target], ranked_by_target[target] = rows, rank_v22_channels(rows)
            progress(f"selection:{target}", saved)
        events: list[dict[str, Any]] = []
        daily_by_target: dict[str, Any] = {}
        for target in V22_MARKET_TARGETS:
            unit, index = f"market:{target}", positions[target]
            if unit in saved:
                daily_by_target[target] = saved[unit]["daily"]
                events.extend(saved[unit]["events"])
                continue
            ranked = ranked_by_target[target]
            daily_by_target[target] = {"trade_date": target, "A_eligible_count": len(ranked["a_pool"]), "B_eligible_count": len(ranked["b_pool"]), "A6": [_public_row(row) for row in ranked["a_selected"]], "B3": [_public_row(row) for row in ranked["b_selected"]], "A_boundary_tie_expanded": ranked["a_boundary_tie_expanded"], "B_boundary_tie_expanded": ranked["b_boundary_tie_expanded"]}
            day = cache[target]
            for channel, pool, selected in (("A", ranked["a_pool"], ranked["a_selected"]), ("B", ranked["b_pool"], ranked["b_selected"])):
                weights = _v22_slot_weights(selected, channel)
                for group, members in _v22_controls(pool, selected, channel).items():
                    for rank, (row, slot_weight) in enumerate(zip(members, weights), 1):
                        events.append(_v22_seed_event(row, group=group, rank=rank, start_index=index, buy=day.get(row["sec_code"], {}).get("buy", {}), signal_stat=day.get(row["sec_code"]), slot_weight=slot_weight))
        dynamic_exit_codes: dict[str, set[str]] = {}
        _v22_resolve_events(events, calendar, load, lambda frontier, open_events: (progress(f"exit:{frontier}", saved, open_events), time_limit()), dynamic_exit_codes)
        all_daily: list[dict[str, Any]] = []
        completed = dict(saved)
        for target in V22_MARKET_TARGETS:
            unit = f"market:{target}"
            if unit in completed:
                continue
            target_events = [event for event in events if event["trade_date"] == target]
            result = {"daily": daily_by_target[target], "eligible_rows": [_public_row(row) for row in ranked_by_target[target]["a_pool"] + ranked_by_target[target]["b_pool"]], "events": target_events, "dynamic_exit_codes_by_date": {day: sorted(codes) for day, codes in sorted(dynamic_exit_codes.items())}, "unit_consumed_input": _v22_checkpoint_identity(records, daily_consumed, target, positions[target], all_rows[target], target_events, calendar)}
            _checkpoint(run_dir, run_hash, unit, result)
            completed[unit] = result
            progress(f"market:{target}", completed, sum(event["outcome_status"] == "open" for event in events))
        for target in V22_MARKET_TARGETS:
            all_daily.extend(completed[f"market:{target}"]["eligible_rows"])
        aggregates = _v22_event_aggregates(events)
        ledger, nav, account = _v22_sleeve_account(events, calendar)
        verified = {unit: _verify_checkpoint_consumed_input(unit, result, records) for unit, result in sorted(completed.items())}
        summary = {"execution_label": "tnm_v22_1_canary_verified", "targets": list(V22_MARKET_TARGETS), "run_hash": run_hash, "spec_hash": manifest["spec_hash"], "base_commit": manifest["base_commit"], "source_blob_identity": manifest["source_blob_identity"], "input_manifest_hash": manifest["input_manifest_hash"], "daily": daily_by_target, "event_count": len(events), "resolved_events": sum(event["outcome_status"] == "resolved" for event in events), "unresolved_events": sum(event["outcome_status"] == "unresolved_at_development_end" for event in events), "aggregates": aggregates, "account": account, "verified_checkpoint_consumed_identities": verified, "consumed_input_identity": _json_identity(verified), "read_2025_2026": False, "e_drive_written": False, "elapsed_seconds": round(time.monotonic() - started, 6), "source_records": records}
        fixture_ok = all(code in {row["sec_code"] for row in daily_by_target[target]["A6"]} for target, code in (("20240923", "300085"), ("20240926", "300339")))
        if not fixture_ok:
            summary["execution_label"] = "diagnostic_changes_required"
        _write_json(run_dir / "v22_summary.json", summary)
        _write_csv_gz(run_dir / "daily_results.csv.gz", all_daily)
        _write_csv_gz(run_dir / "event_results.csv.gz", events)
        _write_csv_gz(run_dir / "account_ledger.csv.gz", ledger)
        _write_csv_gz(run_dir / "account_nav.csv.gz", nav)
        progress("aggregated", completed)
        artifacts = {path.name: {"size_bytes": path.stat().st_size, "sha256": _sha256(path)} for path in sorted(run_dir.iterdir()) if path.is_file() and path.name not in {"artifact_manifest.json", "completion.json"}}
        _write_json(run_dir / "artifact_manifest.json", artifacts)
        _write_json(completion_path, {"status": "SUCCEEDED", "run_hash": run_hash, "execution_label": summary["execution_label"], "artifact_manifest_sha256": _sha256(run_dir / "artifact_manifest.json")})
        return summary, run_dir
    except KeyboardInterrupt:
        if run_dir is not None and not preserve_existing_terminal:
            _write_json(completion_path, {"status": "CANCELLED", "run_hash": run_hash, "reason": "KeyboardInterrupt"})
        raise
    except TailDataError as exc:
        if run_dir is not None and not preserve_existing_terminal and str(exc) == "canary_time_limit":
            _write_json(completion_path, {"status": "CANCELLED", "run_hash": run_hash, "reason": "canary_time_limit"})
        elif run_dir is not None and not preserve_existing_terminal:
            _write_json(completion_path, {"status": "FAILED", "run_hash": run_hash, "reason": type(exc).__name__, "detail": str(exc)})
        raise
    except Exception as exc:
        if run_dir is not None and not preserve_existing_terminal:
            _write_json(completion_path, {"status": "FAILED", "run_hash": run_hash, "reason": type(exc).__name__, "detail": str(exc)})
        raise


def _v23_outcome_journals(
    run_dir: Path, run_hash: str, events: Iterable[Mapping[str, Any]], calendar: list[str],
    records: Mapping[str, Mapping[str, Any]], dynamic_codes: Mapping[str, set[str]],
) -> dict[str, str]:
    """Persist day-owned V2.3 event updates without treating account pauses as event facts."""
    event_by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        for decision in event.get("decisions", []):
            event_by_date[str(decision["trade_date"])].append({"event_id": event["event_id"], "event": _public_row(event)})
    hashes: dict[str, str] = {}
    for trade_date, updates in sorted(event_by_date.items()):
        index = calendar.index(trade_date)
        codes = set(dynamic_codes.get(trade_date, set()))
        dependencies: dict[str, set[str] | None] = {trade_date: codes}
        if index:
            dependencies[calendar[index - 1]] = codes
        journal = {
            "run_hash": run_hash, "trade_date": trade_date, "updates": updates,
            "unit_consumed_input": _unit_consumed_input(_unit_minute_members(records, dependencies), {}),
        }
        path = run_dir / "outcomes" / f"{trade_date}.json"
        _write_json(path, journal)
        hashes[trade_date] = _sha256(path)
    return hashes


def run_v23_canary(
    minute_root: str | Path, daily_root: str | Path, output_dir: str | Path, *, max_elapsed_seconds: float = 900.0,
) -> tuple[dict[str, Any], Path]:
    """The single bounded V2.3 canary: V2.2 selection plus new liquidity, exits and account guard."""
    started, run_dir = time.monotonic(), None
    if float(max_elapsed_seconds) <= 0:
        raise TailDataError("v23_canary_max_elapsed_must_be_positive")
    calendar, positions = _v22_calendar(minute_root)
    if any(target not in positions or positions[target] < 10 for target in V23_MARKET_TARGETS):
        raise TailDataError("v23_canary_calendar_missing_dependency")
    manifest_input = _v22_input_manifest(minute_root, daily_root, calendar)
    run_dir, manifest = _v22_prepare_run(
        output_dir, manifest_input, resume_run_id=None, mode="tnm-v23-1-canary",
        run_prefix="v23-canary", spec_hash=_v23_spec_hash(),
    )
    run_hash, progress_path, completion_path = manifest["run_hash"], run_dir / "progress.json", run_dir / "completion.json"
    cache: dict[str, dict[str, dict[str, Any]]] = {}
    records: dict[str, dict[str, Any]] = {}
    listing_cache: dict[str, dict[str, Any]] = {}
    daily_consumed: dict[str, dict[str, str]] = {}
    completed_sources: list[str] = []
    source_open_counts: dict[str, int] = defaultdict(int)

    def progress(stage: str, completed: Iterable[str], open_events: int = 0) -> None:
        _write_json(progress_path, {
            "run_hash": run_hash, "stage": stage, "completed_units": sorted(completed),
            "total_units": len(V23_MARKET_TARGETS),
            "source_frontier": completed_sources[-1] if completed_sources else None,
            "completed_source_dates": list(completed_sources),
            "source_open_counts": dict(sorted(source_open_counts.items())),
            "open_exit_events": open_events,
            "elapsed_seconds": round(time.monotonic() - started, 6),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })

    def time_limit() -> None:
        if time.monotonic() - started >= float(max_elapsed_seconds):
            raise TailDataError("canary_time_limit")

    def load(trade_date: str, codes: Iterable[str] | None) -> dict[str, dict[str, Any]]:
        requested = None if codes is None else {canonical_code(code) for code in codes}
        if trade_date in cache:
            if requested is not None and not requested.issubset(cache[trade_date]):
                raise TailDataError(f"v23_container_reopen_forbidden:{trade_date}")
            return cache[trade_date]
        values, record = load_day_v2_statistics(minute_root, trade_date, requested)
        cache[trade_date], records[trade_date] = values, record
        completed_sources.append(trade_date)
        source_open_counts[trade_date] += int(record.get("container_open_count", 1))
        progress(f"source:{trade_date}", ())
        time_limit()
        return values

    try:
        progress("input_frozen", ())
        all_rows: dict[str, list[dict[str, Any]]] = {}
        ranked_by_target: dict[str, dict[str, Any]] = {}
        preselected: dict[str, tuple[int, dict[str, dict[str, Any]], set[str]]] = {}
        for target in V23_MARKET_TARGETS:
            index = positions[target]
            day, d1 = load(target, None), load(calendar[index - 1], None)
            pre = {code: _v23_preselection(code, day.get(code), d1.get(code)) for code in day}
            fixture_codes = {code for (fixture_date, code) in V23_FIXTURES if fixture_date == target}
            evaluation = {code for code, proof in pre.items() if proof["survives"]} | fixture_codes
            preselected[target] = (index, day, evaluation)

        evaluation_union = set().union(*(evaluation for _, _, evaluation in preselected.values()))
        shared_history_dates = {
            dependency for _, (index, _, _) in preselected.items()
            for dependency in calendar[index - 10:index - 1]
        }
        for dependency in sorted(shared_history_dates, key=calendar.index):
            load(dependency, evaluation_union)

        for target in V23_MARKET_TARGETS:
            index, day, evaluation = preselected[target]
            d1 = cache[calendar[index - 1]]
            rows = [
                _v23_final_row(
                    code, target, index, day.get(code),
                    [cache[value].get(code) for value in calendar[index - 10:index - 1]] + [d1.get(code)],
                    calendar, daily_root, listing_cache, daily_consumed,
                )
                for code in sorted(evaluation)
            ]
            all_rows[target], ranked_by_target[target] = rows, rank_v22_channels(rows)
            progress(f"selection:{target}", ())

        events: list[dict[str, Any]] = []
        daily_by_target: dict[str, Any] = {}
        for target in V23_MARKET_TARGETS:
            index = positions[target]
            ranked = ranked_by_target[target]
            daily_by_target[target] = {
                "trade_date": target, "A_eligible_count": len(ranked["a_pool"]), "B_eligible_count": len(ranked["b_pool"]),
                "A6": [_public_row(row) for row in ranked["a_selected"]], "B3": [_public_row(row) for row in ranked["b_selected"]],
                "A_boundary_tie_expanded": ranked["a_boundary_tie_expanded"], "B_boundary_tie_expanded": ranked["b_boundary_tie_expanded"],
            }
            day = cache[target]
            for channel, pool, selected in (("A", ranked["a_pool"], ranked["a_selected"]), ("B", ranked["b_pool"], ranked["b_selected"])):
                weights = _v22_slot_weights(selected, channel)
                for group, members in _v22_controls(pool, selected, channel).items():
                    for rank, (row, slot_weight) in enumerate(zip(members, weights), 1):
                        event = _v22_seed_event(
                            row, group=group, rank=rank, start_index=index,
                            buy=day.get(row["sec_code"], {}).get("buy", {}), signal_stat=day.get(row["sec_code"]), slot_weight=slot_weight,
                        )
                        event["event_id"] = f"{target}|{channel}|{group}|{rank}|{event['sec_code']}"
                        events.append(event)

        dynamic_exit_codes: dict[str, set[str]] = {}
        _v23_resolve_events(
            events, calendar, load,
            lambda frontier, open_events: (progress(f"exit:{frontier}", (), open_events), time_limit()),
            dynamic_exit_codes,
        )
        outcome_journals = _v23_outcome_journals(run_dir, run_hash, events, calendar, records, dynamic_exit_codes)
        ledger, nav, account, holdings, risk_states, cooldown_skips = _v23_sleeve_account(events, calendar, load)

        completed: dict[str, dict[str, Any]] = {}
        for target in V23_MARKET_TARGETS:
            unit, index = f"market:{target}", positions[target]
            ranked = ranked_by_target[target]
            target_events = [event for event in events if event["trade_date"] == target]
            result = {
                "daily": daily_by_target[target],
                "eligible_rows": [_public_row(row) for row in ranked["a_pool"] + ranked["b_pool"]],
                "all_candidate_rows": [_public_row(row) for row in all_rows[target]],
                "events": target_events,
                "dynamic_exit_codes_by_date": {day: sorted(codes) for day, codes in sorted(dynamic_exit_codes.items())},
                "unit_consumed_input": _v22_checkpoint_identity(records, daily_consumed, target, index, all_rows[target], target_events, calendar),
            }
            _checkpoint(run_dir, run_hash, unit, result)
            completed[unit] = result
            progress(f"market:{target}", completed, sum(event["outcome_status"] == "open" for event in events))

        fixture_evidence: dict[str, Any] = {}
        for target, code in sorted(V23_FIXTURES):
            row = next((value for value in all_rows[target] if value["sec_code"] == code), None)
            selected = {
                value["sec_code"]
                for value in daily_by_target[target]["A6"] + daily_by_target[target]["B3"]
            }
            expectation = V23_FIXTURES[(target, code)]
            fixture_evidence[f"{code}@{target}"] = {
                "row_present": row is not None,
                "prior5_amount_pass_count": None if row is None else row["prior5_amount_pass_count"],
                "prior5_amount_all_gt_200m": None if row is None else row["prior5_amount_all_gt_200m"],
                "liquidity_pass": None if row is None else row["liquidity_pass"],
                "failure_reasons": None if row is None else row["failure_reasons"],
                "selected_A6_or_B3": code in selected,
                "expected": expectation,
            }
        fixture_ok = (
            fixture_evidence["300085@20240923"]["row_present"]
            and fixture_evidence["300085@20240923"]["prior5_amount_pass_count"] == 1
            and not fixture_evidence["300085@20240923"]["prior5_amount_all_gt_200m"]
            and not fixture_evidence["300085@20240923"]["selected_A6_or_B3"]
            and fixture_evidence["300339@20240926"]["row_present"]
            and fixture_evidence["300339@20240926"]["prior5_amount_pass_count"] == 5
            and fixture_evidence["300339@20240926"]["prior5_amount_all_gt_200m"]
            and fixture_evidence["300339@20240926"]["liquidity_pass"]
        )
        all_daily = [row for target in V23_MARKET_TARGETS for row in completed[f"market:{target}"]["eligible_rows"]]
        verified = {unit: _verify_checkpoint_consumed_input(unit, result, records) for unit, result in sorted(completed.items())}
        aggregates = _v22_event_aggregates(events)
        summary = {
            "execution_label": "tnm_v23_1_canary_verified" if fixture_ok else "changes_required_by_frozen_canary",
            "targets": list(V23_MARKET_TARGETS), "run_hash": run_hash, "spec_hash": manifest["spec_hash"],
            "base_commit": manifest["base_commit"], "source_blob_identity": manifest["source_blob_identity"],
            "input_manifest_hash": manifest["input_manifest_hash"], "daily": daily_by_target,
            "event_count": len(events), "resolved_events": sum(event["outcome_status"] == "resolved" for event in events),
            "unresolved_events": sum(event["outcome_status"] == "unresolved_at_development_end" for event in events),
            "aggregates": aggregates, "account": account, "fixture_evidence": fixture_evidence,
            "outcome_journals": outcome_journals, "verified_checkpoint_consumed_identities": verified,
            "consumed_input_identity": _json_identity(verified), "source_open_counts": dict(sorted(source_open_counts.items())),
            "read_2025_2026": False, "e_drive_written": False,
            "elapsed_seconds": round(time.monotonic() - started, 6), "source_records": records,
        }
        _write_json(run_dir / "v23_summary.json", summary)
        _write_csv_gz(run_dir / "daily_results.csv.gz", all_daily)
        _write_csv_gz(run_dir / "event_results.csv.gz", events)
        _write_csv_gz(run_dir / "account_ledger.csv.gz", ledger)
        _write_csv_gz(run_dir / "account_nav.csv.gz", nav)
        _write_csv_gz(run_dir / "account_holdings_1304.csv.gz", holdings)
        _write_csv_gz(run_dir / "risk_state.csv.gz", risk_states)
        _write_csv_gz(run_dir / "cooldown_skips.csv.gz", cooldown_skips)
        progress("aggregated", completed)
        artifacts = {
            path.relative_to(run_dir).as_posix(): {"size_bytes": path.stat().st_size, "sha256": _sha256(path)}
            for path in sorted(run_dir.rglob("*"))
            if path.is_file() and path.name not in {"artifact_manifest.json", "completion.json"}
        }
        _write_json(run_dir / "artifact_manifest.json", artifacts)
        _write_json(completion_path, {
            "status": "SUCCEEDED", "run_hash": run_hash, "execution_label": summary["execution_label"],
            "artifact_manifest_sha256": _sha256(run_dir / "artifact_manifest.json"),
        })
        return summary, run_dir
    except KeyboardInterrupt:
        if run_dir is not None:
            _write_json(completion_path, {"status": "CANCELLED", "run_hash": run_hash, "reason": "KeyboardInterrupt"})
        raise
    except TailDataError as exc:
        if run_dir is not None:
            status = "CANCELLED" if str(exc) == "canary_time_limit" else "FAILED"
            _write_json(completion_path, {"status": status, "run_hash": run_hash, "reason": str(exc)})
        raise
    except Exception as exc:
        if run_dir is not None:
            _write_json(completion_path, {"status": "FAILED", "run_hash": run_hash, "reason": type(exc).__name__, "detail": str(exc)})
        raise


def _v22_development_claim_lock(run_dir: Path, run_hash: str) -> Path:
    lock = run_dir / "run.lock"
    owner = {"pid": os.getpid(), "run_hash": run_hash, "started_at": datetime.now(timezone.utc).isoformat(), "command": " ".join(os.sys.argv)}
    try:
        with lock.open("x", encoding="utf-8") as handle:
            handle.write(_json_bytes(owner).decode("utf-8"))
    except FileExistsError as exc:
        raise TailDataError("v22_development_lock_exists") from exc
    return lock


def _v22_development_artifacts(run_dir: Path) -> dict[str, dict[str, Any]]:
    return {
        path.relative_to(run_dir).as_posix(): {"size_bytes": path.stat().st_size, "sha256": _sha256(path)}
        for path in sorted(run_dir.rglob("*"))
        if path.is_file() and path.name not in {"artifact_manifest.json", "completion.json", "run.lock"}
    }


def _v22_verify_development_artifacts(run_dir: Path, completion: Mapping[str, Any]) -> None:
    manifest_path = run_dir / "artifact_manifest.json"
    expected = json.loads(manifest_path.read_text(encoding="utf-8"))
    if completion.get("artifact_manifest_sha256") != _sha256(manifest_path) or expected != _v22_development_artifacts(run_dir):
        raise TailDataError("v22_development_artifact_manifest_mismatch")


def _v22_development_outcome_identity(
    records: Mapping[str, Mapping[str, Any]], trade_date: str, previous_date: str, codes: set[str],
) -> dict[str, Any]:
    return _unit_consumed_input(_unit_minute_members(records, {trade_date: codes, previous_date: codes}), {})


def _v22_development_sha_bound_file(run_dir: Path, reference: Mapping[str, Any], kind: str) -> tuple[Path, dict[str, Any]]:
    """Read one marker-owned file and reject a missing or changed payload."""
    try:
        relative = PurePosixPath(str(reference["path"]))
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("non_relative")
        path = run_dir / Path(*relative.parts)
        if path.resolve().parent != (run_dir / Path(*relative.parts[:-1])).resolve():
            raise ValueError("non_relative")
        if not path.is_file() or _sha256(path) != str(reference["sha256"]):
            raise ValueError("hash")
        return path, json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise TailDataError(f"v22_development_daily_commit_{kind}_mismatch") from exc


def _v22_development_read_commits(
    run_dir: Path, run_hash: str, calendar: list[str], positions: Mapping[str, int], *, isolate_orphans: bool,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]], set[str], int]:
    """Return only marker-committed facts; anything else is retained as audit evidence."""
    markers: list[tuple[int, dict[str, Any], dict[str, Any], dict[str, Any] | None, dict[str, Any] | None]] = []
    marker_dir = run_dir / "daily_commits"
    for marker_path in sorted(marker_dir.glob("*.json")) if marker_dir.exists() else []:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        if marker.get("run_hash") != run_hash:
            raise TailDataError("v22_development_daily_commit_hash_mismatch")
        trade_date = str(marker.get("trade_date"))
        state_path, state = _v22_development_sha_bound_file(run_dir, marker.get("state", {}), "state")
        index = int(state.get("last_processed_index", -1))
        if trade_date not in positions or index != positions[trade_date] or state.get("last_processed_date") != trade_date:
            raise TailDataError("v22_development_daily_commit_state_mismatch")
        if state_path.parent != run_dir / "states":
            raise TailDataError("v22_development_daily_commit_state_path")
        signal = None
        if marker.get("signal") is not None:
            _, checkpoint = _v22_development_sha_bound_file(run_dir, marker["signal"], "signal")
            if checkpoint.get("run_hash") != run_hash or checkpoint.get("unit") != f"market:{trade_date}":
                raise TailDataError("v22_development_daily_commit_signal_mismatch")
            signal = dict(checkpoint.get("result", {}))
        outcome = None
        if marker.get("outcome") is not None:
            _, outcome = _v22_development_sha_bound_file(run_dir, marker["outcome"], "outcome")
            if outcome.get("run_hash") != run_hash or str(outcome.get("trade_date")) != trade_date:
                raise TailDataError("v22_development_daily_commit_outcome_mismatch")
        markers.append((index, marker, state, signal, outcome))
    markers.sort(key=lambda value: value[0])
    if [index for index, *_ in markers] != list(range(len(markers))):
        raise TailDataError("v22_development_daily_commit_frontier_gap")
    committed_dates = {str(state["last_processed_date"]) for _, _, state, _, _ in markers}
    if isolate_orphans:
        for directory in ("outcomes", "checkpoints", "states"):
            folder = run_dir / directory
            for path in sorted(folder.glob("*.json")) if folder.exists() else []:
                stem = path.stem.replace("market_", "")
                if stem in committed_dates:
                    continue
                target = run_dir / "orphans" / stem / directory / path.name
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    target = target.with_name(f"{path.stem}.{_sha256(path)[:12]}{path.suffix}")
                os.replace(path, target)
    completed: dict[str, dict[str, Any]] = {}
    journals: list[dict[str, Any]] = []
    outcomes: set[str] = set()
    for _, _, _, signal, outcome in markers:
        if signal is not None:
            completed[f"market:{signal['daily']['trade_date']}"] = signal
        if outcome is not None:
            journals.append(outcome)
            outcomes.add(str(outcome["trade_date"]))
    return completed, journals, outcomes, markers[-1][0] if markers else -1


def _v22_development_required_members(
    completed: Mapping[str, Mapping[str, Any]], journals: Iterable[Mapping[str, Any]],
) -> dict[str, set[str]]:
    required: dict[str, set[str]] = defaultdict(set)
    for result in list(completed.values()) + list(journals):
        identity = result.get("unit_consumed_input", {})
        for day, codes in identity.get("minute_members", {}).items():
            required[str(day)].update(str(code) for code in codes)
    return required


def _v22_development_verify_and_replay(
    completed: Mapping[str, Mapping[str, Any]], journals: Iterable[Mapping[str, Any]], events: list[dict[str, Any]], records: Mapping[str, Mapping[str, Any]],
) -> set[str]:
    """Verify selection/outcome identities against one already-loaded source map."""
    for unit, result in sorted(completed.items()):
        _verify_checkpoint_consumed_input(unit, result, records)
    by_id = {str(event["event_id"]): event for event in events}
    dates: set[str] = set()
    for journal in journals:
        trade_date = str(journal["trade_date"])
        _verify_checkpoint_consumed_input(f"outcome:{trade_date}", {"unit_consumed_input": journal["unit_consumed_input"]}, records)
        for update in journal.get("updates", []):
            event_id = str(update["event_id"])
            if event_id not in by_id:
                raise TailDataError(f"v22_development_outcome_unknown_event:{event_id}")
            by_id[event_id].clear()
            by_id[event_id].update(update["event"])
        dates.add(trade_date)
    return dates


def _v22_development_resume_state(
    run_hash: str, completed: Mapping[str, Mapping[str, Any]], last_index: int,
    calendar: list[str], outcome_dates: Iterable[str], source_open_counts: Mapping[str, int],
) -> dict[str, Any]:
    return {
        "run_hash": run_hash,
        "completed_signal_dates": sorted(str(result["daily"]["trade_date"]) for result in completed.values()),
        "last_processed_index": last_index,
        "last_processed_date": calendar[last_index] if last_index >= 0 else None,
        "outcome_journal_dates": sorted(outcome_dates),
        "source_open_counts": dict(sorted(source_open_counts.items())),
    }


def _v22_development_commit_day(
    run_dir: Path, run_hash: str, trade_date: str, state: Mapping[str, Any], *, outcome: Mapping[str, Any] | None, signal: Mapping[str, Any] | None,
) -> None:
    """Publish one day atomically: only this marker makes its files economic facts."""
    outcome_ref = None
    if outcome is not None:
        path = run_dir / "outcomes" / f"{trade_date}.json"
        _write_json(path, outcome)
        outcome_ref = {"path": path.relative_to(run_dir).as_posix(), "sha256": _sha256(path)}
    signal_ref = None
    if signal is not None:
        path = run_dir / "checkpoints" / f"market_{trade_date}.json"
        _write_json(path, {"run_hash": run_hash, "unit": f"market:{trade_date}", "result": signal})
        signal_ref = {"path": path.relative_to(run_dir).as_posix(), "sha256": _sha256(path)}
    state_path = run_dir / "states" / f"{trade_date}.json"
    _write_json(state_path, dict(state))
    marker = {
        "run_hash": run_hash, "trade_date": trade_date,
        "outcome": outcome_ref, "signal": signal_ref,
        "state": {"path": state_path.relative_to(run_dir).as_posix(), "sha256": _sha256(state_path)},
    }
    _write_json(run_dir / "daily_commits" / f"{trade_date}.json", marker)
    _write_json(run_dir / "resume_state.json", dict(state))


def _v22_development_signal_result(
    records: Mapping[str, Mapping[str, Any]], daily_consumed: Mapping[str, Mapping[str, str]], target: str, index: int,
    rows: list[dict[str, Any]], ranked: Mapping[str, Any], events: list[dict[str, Any]], calendar: list[str],
) -> dict[str, Any]:
    daily = {
        "trade_date": target,
        "A_eligible_count": len(ranked["a_pool"]), "B_eligible_count": len(ranked["b_pool"]),
        "A6": [_public_row(row) for row in ranked["a_selected"]], "B3": [_public_row(row) for row in ranked["b_selected"]],
        "A_boundary_tie_expanded": ranked["a_boundary_tie_expanded"], "B_boundary_tie_expanded": ranked["b_boundary_tie_expanded"],
    }
    selected_a = {str(row["sec_code"]) for row in ranked["a_selected"]}
    selected_b = {str(row["sec_code"]) for row in ranked["b_selected"]}
    eligible_rows = []
    for row in ranked["a_pool"] + ranked["b_pool"]:
        public = _public_row(row)
        public.update({"A_eligible_count": daily["A_eligible_count"], "B_eligible_count": daily["B_eligible_count"], "A6_selected": str(row["sec_code"]) in selected_a, "B3_selected": str(row["sec_code"]) in selected_b})
        eligible_rows.append(public)
    return {
        "daily": daily, "eligible_rows": eligible_rows, "seed_events": [_public_row(event) for event in events],
        "unit_consumed_input": _v22_checkpoint_identity(records, daily_consumed, target, index, rows, events, calendar),
    }


def run_v22_development(minute_root: str | Path, daily_root: str | Path, output_dir: str | Path, *, resume_run_id: str | None = None) -> tuple[dict[str, Any], Path]:
    """One-way 2023--2024 V22 production chain with immutable signal/outcome evidence."""
    started, run_dir, lock = time.monotonic(), None, None
    calendar, positions = _v22_development_calendar(minute_root)
    manifest_input = _v22_development_input_manifest(minute_root, daily_root, calendar)
    run_dir, manifest = _v22_prepare_run(output_dir, manifest_input, resume_run_id=resume_run_id, mode="tnm-v22-development", run_prefix="v22-development", spec_hash=_v22_development_spec_hash())
    run_hash, progress_path, completion_path, resume_path = manifest["run_hash"], run_dir / "progress.json", run_dir / "completion.json", run_dir / "resume_state.json"
    preserve_existing_terminal = completion_path.exists()
    completed: dict[str, dict[str, Any]] = {}
    events: list[dict[str, Any]] = []
    cache: dict[str, dict[str, dict[str, Any]]] = {}
    records: dict[str, dict[str, Any]] = {}
    source_open_counts: dict[str, int] = defaultdict(int)
    outcome_dates: set[str] = set()
    max_cached_days = 0

    def progress(stage: str, last_index: int) -> None:
        _write_json(progress_path, {
            "run_hash": run_hash, "stage": stage, "completed_units": sorted(completed),
            "total_target_dates": sum(index >= 10 and index + 1 < len(calendar) for index in range(len(calendar))),
            "source_frontier": calendar[last_index] if last_index >= 0 else None,
            "source_open_counts": dict(sorted(source_open_counts.items())),
            "cached_market_days": len(cache), "max_cached_market_days": max_cached_days,
            "open_exit_events": sum(event["outcome_status"] == "open" for event in events),
            "elapsed_seconds": round(time.monotonic() - started, 6), "updated_at": datetime.now(timezone.utc).isoformat(),
        })

    def load(index: int) -> dict[str, dict[str, Any]]:
        nonlocal max_cached_days
        trade_date = calendar[index]
        if trade_date in cache:
            return cache[trade_date]
        values, record = load_day_v2_statistics(minute_root, trade_date, None)
        cache[trade_date], records[trade_date] = values, record
        source_open_counts[trade_date] += int(record.get("container_open_count", 1))
        while len(cache) > 11:
            stale = next(iter(cache))
            del cache[stale]
            del records[stale]
        max_cached_days = max(max_cached_days, len(cache))
        return values

    try:
        succeeded_fast_path = False
        if completion_path.exists():
            completion = json.loads(completion_path.read_text(encoding="utf-8"))
            if completion.get("status") == "SUCCEEDED":
                if resume_run_id is None or completion.get("run_hash") != run_hash:
                    raise TailDataError("v22_development_succeeded_requires_explicit_matching_resume")
                succeeded_fast_path = True
            elif completion.get("status") == "FAILED":
                raise TailDataError("v22_development_failed_requires_review")
            elif completion.get("status") != "CANCELLED" or resume_run_id is None:
                raise TailDataError("v22_development_terminal_requires_explicit_resume")
            elif not succeeded_fast_path:
                previous_completion = run_dir / "completion.cancelled.json"
                if not previous_completion.exists():
                    _write_json(previous_completion, completion)
                preserve_existing_terminal = False
        if not succeeded_fast_path:
            lock = _v22_development_claim_lock(run_dir, run_hash)
        if resume_run_id is not None:
            completed, journals, outcome_dates, frontier = _v22_development_read_commits(
                run_dir, run_hash, calendar, positions, isolate_orphans=not succeeded_fast_path,
            )
            for unit, result in sorted(completed.items()):
                if "seed_events" not in result:
                    raise TailDataError(f"v22_development_checkpoint_missing_seeds:{unit}")
                events.extend(dict(event) for event in result["seed_events"])
            if resume_path.exists():
                state = json.loads(resume_path.read_text(encoding="utf-8"))
                if state.get("run_hash") != run_hash:
                    raise TailDataError("v22_development_resume_state_hash_mismatch")
            start_index = frontier + 1
            required = _v22_development_required_members(completed, journals)
            warm_indexes = set(range(max(0, start_index - 10), start_index))
            validation_records: dict[str, dict[str, Any]] = {}
            demands = {positions[day] for day in required}
            for index in sorted(demands | warm_indexes):
                trade_date = calendar[index]
                requested = None if index in warm_indexes else required[trade_date]
                values, record = load_day_v2_statistics(minute_root, trade_date, requested)
                source_open_counts[trade_date] += int(record.get("container_open_count", 1))
                if index in warm_indexes:
                    cache[trade_date], records[trade_date] = values, record
                else:
                    validation_records[trade_date] = record
            max_cached_days = len(cache)
            source_records = dict(validation_records) | records
            outcome_dates = _v22_development_verify_and_replay(completed, journals, events, source_records)
        else:
            start_index = 0
        if succeeded_fast_path:
            _v22_verify_development_artifacts(run_dir, completion)
            return json.loads((run_dir / "development_summary.json").read_text(encoding="utf-8")), run_dir
        progress("input_frozen" if start_index == 0 else "resumed", start_index - 1)
        for index in range(start_index, len(calendar)):
            day = load(index)
            journal: dict[str, Any] | None = None
            if index:
                open_events = _v22_apply_exit_day(events, calendar, index, day, cache[calendar[index - 1]])
                if open_events:
                    codes = {str(event["sec_code"]) for event in open_events}
                    journal = {
                        "run_hash": run_hash, "trade_date": calendar[index],
                        "updates": [{"event_id": event["event_id"], "event": _public_row(event)} for event in open_events],
                        "unit_consumed_input": _v22_development_outcome_identity(records, calendar[index], calendar[index - 1], codes),
                    }
                    outcome_dates.add(calendar[index])
            result: dict[str, Any] | None = None
            if index >= 10 and index + 1 < len(calendar):
                target, unit = calendar[index], f"market:{calendar[index]}"
                if unit not in completed:
                    d1 = cache[calendar[index - 1]]
                    pre = {code: _v22_preselection(code, day.get(code), d1.get(code)) for code in day}
                    evaluation = sorted(code for code, proof in pre.items() if proof["survives"])
                    listing_cache: dict[str, dict[str, Any]] = {}
                    daily_consumed: dict[str, dict[str, str]] = {}
                    rows = [_v22_final_row(code, target, index, day.get(code), [cache[value].get(code) for value in calendar[index - 10:index - 1]] + [d1.get(code)], daily_root, listing_cache, daily_consumed) for code in evaluation]
                    ranked = rank_v22_channels(rows)
                    target_events: list[dict[str, Any]] = []
                    for channel, pool, selected in (("A", ranked["a_pool"], ranked["a_selected"]), ("B", ranked["b_pool"], ranked["b_selected"])):
                        weights = _v22_slot_weights(selected, channel)
                        for group, members in _v22_controls(pool, selected, channel).items():
                            for rank, (row, slot_weight) in enumerate(zip(members, weights), 1):
                                event = _v22_seed_event(row, group=group, rank=rank, start_index=index, buy=day.get(row["sec_code"], {}).get("buy", {}), signal_stat=day.get(row["sec_code"]), slot_weight=slot_weight)
                                event["event_id"] = f"{target}|{channel}|{group}|{rank}|{event['sec_code']}"
                                target_events.append(event)
                    result = _v22_development_signal_result(records, daily_consumed, target, index, rows, ranked, target_events, calendar)
                    completed[unit] = result
                    events.extend(target_events)
            state = _v22_development_resume_state(run_hash, completed, index, calendar, outcome_dates, source_open_counts)
            _v22_development_commit_day(run_dir, run_hash, calendar[index], state, outcome=journal, signal=result)
            progress(f"source:{calendar[index]}", index)
        for event in events:
            if event["outcome_status"] == "open":
                event.update({"outcome_status": "unresolved_at_development_end", "holding_days": len(calendar) - int(event["start_index"]) - 1})
        all_daily = [row for _, result in sorted(completed.items()) for row in result["eligible_rows"]]
        daily = {str(result["daily"]["trade_date"]): result["daily"] for _, result in sorted(completed.items())}
        aggregates = _v22_event_aggregates(events)
        ledger, nav, account = _v22_sleeve_account(events, calendar)
        verified = {unit: str(result["unit_consumed_input"]["unit_consumed_input_identity"]) for unit, result in sorted(completed.items())}
        strategy = [event for event in events if event["strategy_or_control"] == "strategy"]
        summary = {
            "execution_label": "tnm_v22_development_completed", "targets": sorted(daily), "target_count": len(daily),
            "run_hash": run_hash, "spec_hash": manifest["spec_hash"], "base_commit": manifest["base_commit"],
            "source_blob_identity": manifest["source_blob_identity"], "input_manifest_hash": manifest["input_manifest_hash"],
            "daily": daily, "event_count": len(events), "resolved_events": sum(event["outcome_status"] == "resolved" for event in events),
            "unresolved_events": sum(event["outcome_status"] == "unresolved_at_development_end" for event in events),
            "strategy_channel_summary": {
                "A": _v22_group_metrics(event for event in strategy if event["channel"] == "A"),
                "B": _v22_group_metrics(event for event in strategy if event["channel"] == "B"),
                "combined": _v22_group_metrics(strategy),
                "AB": aggregates["combined_channel"]["overall"].get("strategy", _v22_group_metrics(())),
            },
            "aggregates": aggregates, "account": account, "verified_checkpoint_consumed_identities": verified,
            "consumed_input_identity": _json_identity(verified), "source_open_counts": dict(sorted(source_open_counts.items())),
            "max_cached_market_days": max_cached_days,
            "read_2025_2026": False, "e_drive_written": False, "elapsed_seconds": round(time.monotonic() - started, 6),
        }
        _write_json(run_dir / "development_summary.json", summary)
        _write_csv_gz(run_dir / "daily_results.csv.gz", all_daily)
        _write_csv_gz(run_dir / "event_results.csv.gz", events)
        _write_csv_gz(run_dir / "account_ledger.csv.gz", ledger)
        _write_csv_gz(run_dir / "account_nav.csv.gz", nav)
        progress("aggregated", len(calendar) - 1)
        _write_json(run_dir / "artifact_manifest.json", _v22_development_artifacts(run_dir))
        _write_json(completion_path, {"status": "SUCCEEDED", "run_hash": run_hash, "execution_label": summary["execution_label"], "artifact_manifest_sha256": _sha256(run_dir / "artifact_manifest.json")})
        return summary, run_dir
    except KeyboardInterrupt:
        if run_dir is not None and not preserve_existing_terminal:
            _write_json(completion_path, {"status": "CANCELLED", "run_hash": run_hash, "reason": "KeyboardInterrupt"})
        raise
    except Exception as exc:
        if run_dir is not None and not preserve_existing_terminal:
            _write_json(completion_path, {"status": "FAILED", "run_hash": run_hash, "reason": type(exc).__name__, "detail": str(exc)})
        raise
    finally:
        if lock is not None and lock.exists():
            lock.unlink()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TNM V2 R1 diagnostic only")
    subparsers = parser.add_subparsers(dest="command", required=True)
    diagnostic = subparsers.add_parser("diagnostic")
    diagnostic.add_argument("--minute-root", required=True)
    diagnostic.add_argument("--daily-root", required=True)
    diagnostic.add_argument("--output-dir", required=True)
    diagnostic.add_argument("--resume-run-id")
    v22 = subparsers.add_parser("v22-canary")
    v22.add_argument("--minute-root", required=True)
    v22.add_argument("--daily-root", required=True)
    v22.add_argument("--output-dir", required=True)
    v22.add_argument("--resume-run-id")
    v22.add_argument("--max-elapsed-seconds", type=float, default=900.0)
    v23 = subparsers.add_parser("v23-canary")
    v23.add_argument("--minute-root", required=True)
    v23.add_argument("--daily-root", required=True)
    v23.add_argument("--output-dir", required=True)
    v23.add_argument("--max-elapsed-seconds", type=float, default=900.0)
    development = subparsers.add_parser("v22-development")
    development.add_argument("--minute-root", required=True)
    development.add_argument("--daily-root", required=True)
    development.add_argument("--output-dir", required=True)
    development.add_argument("--resume-run-id")
    args = parser.parse_args(argv)
    if args.command == "diagnostic":
        summary, run_dir = run_diagnostic(args.minute_root, args.daily_root, args.output_dir, resume_run_id=args.resume_run_id)
    elif args.command == "v22-canary":
        summary, run_dir = run_v22_canary(args.minute_root, args.daily_root, args.output_dir, resume_run_id=args.resume_run_id, max_elapsed_seconds=args.max_elapsed_seconds)
    elif args.command == "v23-canary":
        summary, run_dir = run_v23_canary(args.minute_root, args.daily_root, args.output_dir, max_elapsed_seconds=args.max_elapsed_seconds)
    elif args.command == "v22-development":
        summary, run_dir = run_v22_development(args.minute_root, args.daily_root, args.output_dir, resume_run_id=args.resume_run_id)
    else:
        raise TailDataError("unsupported_v2_command")
    print(f"status={summary['execution_label']} run_dir={run_dir}")
    return 0 if summary["execution_label"] in {"tnm_v2_r1_diagnostic_ready", "tnm_v22_1_canary_verified", "tnm_v22_development_completed", "tnm_v23_1_canary_verified"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
