from __future__ import annotations

import datetime as dt
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Callable

import pandas as pd

from .data_quality import (
    STATUS_CLEAN,
    STATUS_USABLE_WITH_QUARANTINE,
    clean_stock_trade_dates,
    inspect_stock_session,
)
from .selection_context import SelectionContext, build_selection_context


MODE_INTRADAY = "intraday_snapshot"
MODE_CLOSE_PENDING = "close_pending"
MODE_CLOSE_FINAL = "close_final"
MODE_DATA_UNAVAILABLE = "data_unavailable"
CHINA_TZ = dt.timezone(dt.timedelta(hours=8))


@dataclass(frozen=True)
class SnapshotPolicy:
    max_age_minutes: int = 10
    min_coverage_ratio: float = 0.8
    close_time: dt.time = dt.time(15, 0)


@dataclass(frozen=True)
class ModeResolution:
    mode: str
    reason: str
    snapshot_coverage: float | None = None


@dataclass
class RuntimeResult:
    mode: str
    context: SelectionContext | None
    reason: str
    snapshot_source: str | None
    snapshot_coverage: float | None
    cache_key: tuple[str, str, str] | None
    official: bool
    finalization_result: Any = None


def _as_china_time(value: dt.datetime) -> dt.datetime:
    return value.replace(tzinfo=CHINA_TZ) if value.tzinfo is None else value.astimezone(CHINA_TZ)


def _parse_snapshot_as_of(value: str | dt.datetime) -> dt.datetime:
    if isinstance(value, dt.datetime):
        return _as_china_time(value)
    parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return _as_china_time(parsed)


def resolve_selection_mode(
    *,
    trade_date: str,
    now: dt.datetime,
    close_quality_status: str,
    snapshot_as_of: str | dt.datetime | None,
    snapshot_coverage: float | None,
    policy: SnapshotPolicy = SnapshotPolicy(),
) -> ModeResolution:
    """Resolve the only legal runtime mode; finalized close data always wins."""
    current = _as_china_time(now)
    if close_quality_status in {STATUS_CLEAN, STATUS_USABLE_WITH_QUARANTINE}:
        reason = "clean_close_final" if close_quality_status == STATUS_CLEAN else "usable_with_quarantine_close_final"
        return ModeResolution(MODE_CLOSE_FINAL, reason)
    if current.date().isoformat() != str(trade_date) or current.weekday() >= 5:
        return ModeResolution(MODE_DATA_UNAVAILABLE, "snapshot_not_valid_after_trade_date")
    if snapshot_as_of is None or snapshot_coverage is None:
        return ModeResolution(MODE_DATA_UNAVAILABLE, "snapshot_missing")
    snapshot_time = _parse_snapshot_as_of(snapshot_as_of)
    age_minutes = (current - snapshot_time).total_seconds() / 60.0
    if age_minutes < 0 or age_minutes > int(policy.max_age_minutes):
        return ModeResolution(MODE_DATA_UNAVAILABLE, "snapshot_stale", snapshot_coverage)
    if snapshot_coverage < float(policy.min_coverage_ratio):
        return ModeResolution(MODE_DATA_UNAVAILABLE, "snapshot_coverage_below_threshold", snapshot_coverage)
    mode = MODE_CLOSE_PENDING if current.time().replace(tzinfo=None) >= policy.close_time else MODE_INTRADAY
    return ModeResolution(mode, "snapshot_usable", snapshot_coverage)


def _snapshot_coverage(conn: sqlite3.Connection, snapshot: pd.DataFrame, reference_date: str) -> float:
    expected_row = conn.execute(
        "SELECT COUNT(DISTINCT sec_code) FROM ash.kline_daily WHERE sec_type='stock' AND trade_date=?",
        (str(reference_date),),
    ).fetchone()
    expected = int(expected_row[0] or 0) if expected_row is not None else 0
    if expected <= 0 or "sec_code" not in snapshot.columns:
        return 0.0
    snapshot_codes = snapshot["sec_code"].astype(str).str.zfill(6).drop_duplicates()
    expected_codes = {
        str(row[0]).zfill(6)
        for row in conn.execute(
            "SELECT DISTINCT sec_code FROM ash.kline_daily WHERE sec_type='stock' AND trade_date=?",
            (str(reference_date),),
        )
    }
    return len(set(snapshot_codes).intersection(expected_codes)) / expected


def _empty_unavailable_context(trade_date: str, resolution: ModeResolution) -> SelectionContext:
    return SelectionContext(
        bars=pd.DataFrame(),
        universe=pd.DataFrame(),
        trade_date=str(trade_date),
        mode=MODE_DATA_UNAVAILABLE,
        as_of=str(trade_date),
        price_as_of=str(trade_date),
        metadata_as_of=None,
        trend_profile=None,
        data_status=MODE_DATA_UNAVAILABLE,
        diagnostics={"runtime_reason": resolution.reason, "snapshot_coverage": resolution.snapshot_coverage},
    )


class SelectionRuntime:
    """Process-local snapshot cache; only close-final contexts may invoke a finalizer."""

    def __init__(self, policy: SnapshotPolicy = SnapshotPolicy()) -> None:
        self.policy = policy
        self._snapshot_cache: dict[tuple[str, str, str], SelectionContext] = {}

    def run(
        self,
        conn: sqlite3.Connection,
        trade_date: str,
        *,
        now: dt.datetime,
        snapshot_bars: pd.DataFrame | None = None,
        snapshot_benchmark_closes: Mapping[str, object] | None = None,
        snapshot_as_of: str | dt.datetime | None = None,
        snapshot_source: str | None = None,
        finalizer: Callable[[SelectionContext], Any] | None = None,
    ) -> RuntimeResult:
        quality = inspect_stock_session(conn, trade_date)
        clean_baseline_dates = clean_stock_trade_dates(conn, end_date=str(trade_date), include_end=False)
        reference_date = clean_baseline_dates[-1] if clean_baseline_dates else None
        coverage = (
            _snapshot_coverage(conn, pd.DataFrame(snapshot_bars), reference_date)
            if snapshot_bars is not None and reference_date is not None
            else None
        )
        resolution = resolve_selection_mode(
            trade_date=str(trade_date),
            now=now,
            close_quality_status=str(quality["status"]),
            snapshot_as_of=snapshot_as_of,
            snapshot_coverage=coverage,
            policy=self.policy,
        )
        if resolution.mode == MODE_DATA_UNAVAILABLE:
            return RuntimeResult(
                mode=resolution.mode,
                context=_empty_unavailable_context(trade_date, resolution),
                reason=resolution.reason,
                snapshot_source=snapshot_source,
                snapshot_coverage=coverage,
                cache_key=None,
                official=False,
            )
        if resolution.mode == MODE_CLOSE_FINAL:
            context = build_selection_context(
                conn,
                str(trade_date),
                mode=MODE_CLOSE_FINAL,
                as_of=_as_china_time(now).isoformat(),
                price_as_of=str(trade_date),
            )
            for key in [key for key in self._snapshot_cache if key[0] == str(trade_date)]:
                del self._snapshot_cache[key]
            finalization_result = finalizer(context) if finalizer is not None else None
            return RuntimeResult(
                mode=resolution.mode,
                context=context,
                reason=resolution.reason,
                snapshot_source=None,
                snapshot_coverage=None,
                cache_key=None,
                official=True,
                finalization_result=finalization_result,
            )

        snapshot = pd.DataFrame(snapshot_bars).copy()
        snapshot["trade_date"] = str(trade_date)
        snapshot_names = snapshot[["sec_code", "sec_name"]] if "sec_name" in snapshot.columns else None
        snapshot_time = _parse_snapshot_as_of(snapshot_as_of)
        cache_key = (str(trade_date), snapshot_time.isoformat(), resolution.mode)
        cached = self._snapshot_cache.get(cache_key)
        if cached is None:
            context = build_selection_context(
                conn,
                str(trade_date),
                mode=resolution.mode,
                as_of=snapshot_time.isoformat(),
                price_as_of=snapshot_time.isoformat(),
                snapshot_names=snapshot_names,
                clean_dates=clean_baseline_dates,
                current_bars=snapshot,
                current_benchmark_closes=snapshot_benchmark_closes,
                allow_missing_activity=True,
            )
            context.diagnostics.update(
                {
                    "runtime_reason": resolution.reason,
                    "snapshot_source": snapshot_source,
                    "snapshot_coverage": coverage,
                    "snapshot_as_of": snapshot_time.isoformat(),
                    "activity_unavailable": bool(context.universe.get("activity_available", pd.Series(dtype=bool)).eq(False).any()),
                }
            )
            self._snapshot_cache[cache_key] = context
        else:
            context = cached
        return RuntimeResult(
            mode=resolution.mode,
            context=context,
            reason=resolution.reason,
            snapshot_source=snapshot_source,
            snapshot_coverage=coverage,
            cache_key=cache_key,
            official=False,
        )
