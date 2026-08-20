from __future__ import annotations

import datetime as dt
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

CALENDAR_SCHEMA_VERSION = 1
CALENDAR_FILE_NAME = "a_share_trade_calendar.json"
ALLOWED_SOURCES = frozenset({"akshare", "baostock"})


@dataclass(frozen=True)
class PreviousTradeDate:
    trade_date: str | None
    reason: str | None


def calendar_path(base_dir: str | Path) -> Path:
    return Path(base_dir) / "data" / CALENDAR_FILE_NAME


def _parse_date(value: object) -> str | None:
    try:
        return dt.date.fromisoformat(str(value)).isoformat()
    except (TypeError, ValueError):
        return None


def _parse_datetime(value: object) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=dt.timezone.utc)


def write_trade_calendar(
    base_dir: str | Path,
    *,
    source: str,
    open_dates: Iterable[object],
    retrieved_at: dt.datetime | None = None,
) -> dict[str, Any]:
    """Atomically persist provider-confirmed open dates; callers own provider access."""
    if source not in ALLOWED_SOURCES:
        raise ValueError("unsupported A-share calendar source")
    dates = sorted({day for value in open_dates if (day := _parse_date(value))})
    if not dates:
        raise ValueError("calendar must contain at least one open date")
    observed = retrieved_at or dt.datetime.now(dt.timezone.utc)
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=dt.timezone.utc)
    payload = {
        "schema_version": CALENDAR_SCHEMA_VERSION,
        "source": source,
        "retrieved_at": observed.isoformat(),
        "coverage_end": dates[-1],
        "open_dates": dates,
    }
    path = calendar_path(base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)
    return payload


def previous_trade_date(
    base_dir: str | Path,
    *,
    current_date: object,
    now: dt.datetime | None = None,
) -> PreviousTradeDate:
    """Read only a recent, provider-confirmed calendar; never approximate weekdays."""
    current = _parse_date(current_date)
    if current is None:
        return PreviousTradeDate(None, "expected_trade_calendar_invalid_current_date")
    path = calendar_path(base_dir)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return PreviousTradeDate(None, "expected_trade_calendar_unavailable")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return PreviousTradeDate(None, "expected_trade_calendar_invalid")

    if not isinstance(payload, dict):
        return PreviousTradeDate(None, "expected_trade_calendar_invalid")
    if payload.get("schema_version") != CALENDAR_SCHEMA_VERSION or payload.get("source") not in ALLOWED_SOURCES:
        return PreviousTradeDate(None, "expected_trade_calendar_invalid")
    retrieved_at = _parse_datetime(payload.get("retrieved_at"))
    coverage_end = _parse_date(payload.get("coverage_end"))
    dates_value = payload.get("open_dates")
    if retrieved_at is None or coverage_end is None or not isinstance(dates_value, list):
        return PreviousTradeDate(None, "expected_trade_calendar_invalid")

    observed = now or dt.datetime.now(dt.timezone.utc)
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=dt.timezone.utc)
    if retrieved_at < observed.astimezone(dt.timezone.utc) - dt.timedelta(days=14):
        return PreviousTradeDate(None, "expected_trade_calendar_stale")
    current_day = dt.date.fromisoformat(current)
    if dt.date.fromisoformat(coverage_end) < current_day - dt.timedelta(days=1):
        return PreviousTradeDate(None, "expected_trade_calendar_stale")

    dates = sorted({day for value in dates_value if (day := _parse_date(value))})
    prior = [day for day in dates if day < current]
    if not prior:
        return PreviousTradeDate(None, "expected_trade_calendar_missing_prior")
    return PreviousTradeDate(prior[-1], None)
