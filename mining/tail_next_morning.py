"""Fail-closed primitives for the Tail Next-Morning V1 research card.

This module deliberately stops at source validation and a fixed small preflight.
It does not select features, run a historical batch, write a database, or call a
provider.  Feature snapshots only receive information available at D 14:50;
outcomes are calculated by a separate function.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping

import pandas as pd


MINUTE_COLUMNS = ("open", "high", "low", "close", "amount", "volume")
A_SHARE_PREFIXES = ("000", "001", "002", "003", "300", "301", "600", "601", "603", "605", "688", "689")
MIN_HISTORY_SESSIONS = 20
MIN_D1_AMOUNT = 500_000_000.0
CAPACITY_TOTAL_YUAN = 5_000_000.0
CAPACITY_PER_STOCK_YUAN = 500_000.0
CAPACITY_PARTICIPATION_LIMIT = 0.005
DAILY_MINUTE_AMOUNT_RATIO_TOLERANCE = 0.01


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


def normalize_minute_frame(raw: pd.DataFrame) -> pd.DataFrame:
    """Validate the fixed 240-bar positional minute-source contract."""
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
        if value.isna().any() or not value.map(math.isfinite).all():
            raise TailDataError(f"non_numeric_minute_{name}")
        result[name] = value.astype(float)

    prices = result[["open", "high", "low", "close"]]
    if prices.le(0).any().any():
        raise TailDataError("non_positive_ohlc")
    if result[["amount", "volume"]].lt(0).any().any():
        raise TailDataError("negative_amount_or_volume")
    if (result["high"] < prices[["open", "close", "low"]].max(axis=1)).any() or (
        result["low"] > prices[["open", "close", "high"]].min(axis=1)
    ).any():
        raise TailDataError("invalid_ohlc_range")
    return result.reset_index(drop=True)


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
    return normalize_minute_frame(raw)


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


def outcome_snapshot(day_bars: pd.DataFrame, next_day_bars: pd.DataFrame) -> dict[str, Any]:
    """Compute labels and executable VWAPs; this path never feeds feature eligibility."""
    buy = vwap_for_window(day_bars, "14:51", "14:56")
    if buy["status"] != "ready":
        return {
            "outcome_status": "cash_unfilled_buy",
            "buy": buy,
            "sell": None,
            "gross_return": None,
            "net_return_15bps": None,
            "net_return_30bps": None,
            "mfe_0930_1000": None,
            "mae_0930_1000": None,
        }
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


def daily_minute_amount_ratio(daily_amount: float | None, bars: pd.DataFrame) -> float | None:
    minute_amount = float(bars["amount"].sum())
    if daily_amount is None or not math.isfinite(float(daily_amount)) or minute_amount <= 0:
        return None
    return float(daily_amount) / minute_amount


def daily_minute_crosscheck(daily_amount: float | None, bars: pd.DataFrame) -> dict[str, Any]:
    ratio = daily_minute_amount_ratio(daily_amount, bars)
    if ratio is None:
        return {"status": "unavailable", "ratio": None}
    status = "ready" if abs(ratio - 1.0) <= DAILY_MINUTE_AMOUNT_RATIO_TOLERANCE else "mismatch"
    return {"status": status, "ratio": ratio}


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
        required_days = [sessions_by_day[day].get(code) for day in sample["dates"]]
        if any(session is None for session in required_days):
            rows.append({"sec_code": code, "sample_status": "isolated_missing_or_invalid_session"})
            continue
        d3, d2, d1, day, next_day = required_days
        listing = listing_evidence(daily_root, code, target, minute_visible_sessions=3)
        snapshot = feature_snapshot(code, day, (d3, d2, d1), listing)
        outcome = outcome_snapshot(day, next_day)
        amount_crosscheck = daily_minute_crosscheck(listing.get("daily_amount"), day)
        if amount_crosscheck["status"] != "ready":
            sample_errors.append(f"daily_minute_amount_{amount_crosscheck['status']}:{code}")
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
    args = parser.parse_args(argv)
    if args.command == "preflight":
        result, report_path, digest = run_preflight(args.minute_root, args.daily_root, args.output_dir)
        print(
            f"status={result['preflight_status']} label={result['execution_label']} "
            f"report={report_path} sha256={digest}"
        )
        return 0 if result["preflight_status"] == "verified" else 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
