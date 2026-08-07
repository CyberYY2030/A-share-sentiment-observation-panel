from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

import pandas as pd

from .intraday import _call_endpoint_with_timeout, normalize_spot_frame


DEFAULT_PROVIDERS = ("stock_zh_a_spot_em", "stock_zh_a_spot")
CHINA_TZ = dt.timezone(dt.timedelta(hours=8))


@dataclass(frozen=True)
class QuoteSnapshotResult:
    frame: pd.DataFrame
    provider: str | None
    observed_at: str | None
    raw_rows: int
    normalized_rows: int
    coverage: float
    attempts: int
    errors: tuple[str, ...]
    status: str
    from_cache: bool
    retry_at: str | None = None
    benchmark_closes: dict[str, float] = field(default_factory=dict)


ProviderFetcher = Callable[[str, float], pd.DataFrame]


def _as_china_time(value: dt.datetime) -> dt.datetime:
    return value.replace(tzinfo=CHINA_TZ) if value.tzinfo is None else value.astimezone(CHINA_TZ)


def _default_provider_fetcher(provider: str, timeout_seconds: float) -> pd.DataFrame:
    return _call_endpoint_with_timeout(provider, timeout_seconds)


def _canonical_codes(values: Iterable[object]) -> set[str]:
    return {str(value).zfill(6) for value in values if str(value).strip()}


class QuoteSnapshotAdapter:
    """One bounded quote path with explicit failover, cooling, and restart cache rules."""

    def __init__(
        self,
        *,
        cache_dir: str | Path,
        provider_names: tuple[str, ...] = DEFAULT_PROVIDERS,
        provider_fetcher: ProviderFetcher = _default_provider_fetcher,
        timeout_seconds: float = 8.0,
        cooldown_seconds: float = 60.0,
        max_age_minutes: int = 10,
        min_coverage_ratio: float = 0.80,
    ) -> None:
        if not provider_names:
            raise ValueError("provider_names must not be empty")
        if not 5.0 <= float(timeout_seconds) <= 8.0:
            raise ValueError("timeout_seconds must be between 5 and 8 seconds")
        self.cache_dir = Path(cache_dir)
        self.provider_names = tuple(provider_names)
        self.provider_fetcher = provider_fetcher
        self.timeout_seconds = float(timeout_seconds)
        self.cooldown_seconds = float(cooldown_seconds)
        self.max_age_minutes = int(max_age_minutes)
        self.min_coverage_ratio = float(min_coverage_ratio)
        self._failed_until: dict[tuple[str, tuple[str, ...]], dt.datetime] = {}

    def _cache_paths(self, trade_date: str) -> tuple[Path, Path]:
        root = self.cache_dir / str(trade_date)
        return root / "snapshot.pkl", root / "metadata.json"

    def _coverage(self, frame: pd.DataFrame, expected_codes: set[str]) -> float:
        if not expected_codes or "sec_code" not in frame.columns:
            return 0.0
        observed = _canonical_codes(frame["sec_code"])
        return len(observed.intersection(expected_codes)) / len(expected_codes)

    def _save_success(self, trade_date: str, result: QuoteSnapshotResult) -> None:
        frame_path, metadata_path = self._cache_paths(trade_date)
        frame_path.parent.mkdir(parents=True, exist_ok=True)
        temp_frame = frame_path.with_suffix(".tmp.pkl")
        temp_metadata = metadata_path.with_suffix(".tmp.json")
        result.frame.to_pickle(temp_frame)
        temp_metadata.write_text(
            json.dumps(
                {
                    "trade_date": str(trade_date),
                    "provider": result.provider,
                    "observed_at": result.observed_at,
                    "raw_rows": result.raw_rows,
                    "normalized_rows": result.normalized_rows,
                    "coverage": result.coverage,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        temp_frame.replace(frame_path)
        temp_metadata.replace(metadata_path)

    def _cached_result(
        self,
        trade_date: str,
        expected_codes: set[str],
        now: dt.datetime,
        *,
        close_final: bool,
    ) -> QuoteSnapshotResult | None:
        if close_final:
            return None
        frame_path, metadata_path = self._cache_paths(trade_date)
        if not frame_path.exists() or not metadata_path.exists():
            return None
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            if str(metadata.get("trade_date")) != str(trade_date):
                return None
            observed_at = dt.datetime.fromisoformat(str(metadata["observed_at"]))
            observed_at = _as_china_time(observed_at)
            age_minutes = (now - observed_at).total_seconds() / 60.0
            frame = pd.read_pickle(frame_path)
            coverage = self._coverage(frame, expected_codes)
            if age_minutes < 0 or age_minutes > self.max_age_minutes:
                return QuoteSnapshotResult(
                    frame=pd.DataFrame(),
                    provider=None,
                    observed_at=metadata.get("observed_at"),
                    raw_rows=0,
                    normalized_rows=0,
                    coverage=coverage,
                    attempts=0,
                    errors=("cached snapshot exceeded max age",),
                    status="snapshot_stale",
                    from_cache=False,
                )
            if coverage < self.min_coverage_ratio:
                return QuoteSnapshotResult(
                    frame=pd.DataFrame(),
                    provider=None,
                    observed_at=metadata.get("observed_at"),
                    raw_rows=0,
                    normalized_rows=0,
                    coverage=coverage,
                    attempts=0,
                    errors=("cached snapshot coverage below threshold",),
                    status="coverage_below_threshold",
                    from_cache=False,
                )
            return QuoteSnapshotResult(
                frame=frame.copy(),
                provider=str(metadata.get("provider") or "cache"),
                observed_at=metadata.get("observed_at"),
                raw_rows=int(metadata.get("raw_rows") or len(frame)),
                normalized_rows=len(frame),
                coverage=coverage,
                attempts=0,
                errors=(),
                status="snapshot_cache_fallback",
                from_cache=True,
            )
        except Exception as exc:
            return QuoteSnapshotResult(
                frame=pd.DataFrame(),
                provider=None,
                observed_at=None,
                raw_rows=0,
                normalized_rows=0,
                coverage=0.0,
                attempts=0,
                errors=(f"cache_read_failed:{type(exc).__name__}",),
                status="provider_failed",
                from_cache=False,
            )

    def _failure_result(
        self,
        *,
        status: str,
        errors: list[str],
        attempts: int,
        now: dt.datetime,
    ) -> QuoteSnapshotResult:
        retry_at = now + dt.timedelta(seconds=self.cooldown_seconds)
        return QuoteSnapshotResult(
            frame=pd.DataFrame(),
            provider=None,
            observed_at=now.isoformat(),
            raw_rows=0,
            normalized_rows=0,
            coverage=0.0,
            attempts=attempts,
            errors=tuple(errors),
            status=status,
            from_cache=False,
            retry_at=retry_at.isoformat(),
        )

    def load(
        self,
        trade_date: str,
        *,
        expected_codes: Iterable[object],
        now: dt.datetime,
        close_final: bool = False,
    ) -> QuoteSnapshotResult:
        current = _as_china_time(now)
        expected = _canonical_codes(expected_codes)
        fingerprint = (str(trade_date), tuple(sorted(expected)))
        cooldown_until = self._failed_until.get(fingerprint)
        if cooldown_until is not None and current < cooldown_until:
            cached = self._cached_result(str(trade_date), expected, current, close_final=close_final)
            if cached is not None:
                return cached
            return self._failure_result(
                status="provider_failed",
                errors=("provider cooldown active",),
                attempts=0,
                now=current,
            )

        errors: list[str] = []
        saw_empty = False
        saw_normalization_empty = False
        saw_low_coverage = False
        raw_rows = 0
        for attempt, provider in enumerate(self.provider_names, start=1):
            try:
                raw = pd.DataFrame(self.provider_fetcher(provider, self.timeout_seconds))
                raw_rows = len(raw)
                if raw.empty:
                    saw_empty = True
                    errors.append(f"{provider}:empty_payload")
                    continue
                normalized = normalize_spot_frame(raw)
                if normalized.empty:
                    saw_normalization_empty = True
                    errors.append(f"{provider}:normalization_empty")
                    continue
                coverage = self._coverage(normalized, expected)
                if coverage < self.min_coverage_ratio:
                    saw_low_coverage = True
                    errors.append(f"{provider}:coverage_below_threshold:{coverage:.3f}")
                    continue
                result = QuoteSnapshotResult(
                    frame=normalized.copy(),
                    provider=provider,
                    observed_at=current.isoformat(),
                    raw_rows=raw_rows,
                    normalized_rows=len(normalized),
                    coverage=coverage,
                    attempts=attempt,
                    errors=tuple(errors),
                    status="snapshot_usable",
                    from_cache=False,
                )
                self._failed_until.pop(fingerprint, None)
                self._save_success(str(trade_date), result)
                return result
            except Exception as exc:
                errors.append(f"{provider}:{type(exc).__name__}:{exc}")

        cached = self._cached_result(str(trade_date), expected, current, close_final=close_final)
        if cached is not None and cached.status in {"snapshot_cache_fallback", "snapshot_stale", "coverage_below_threshold"}:
            self._failed_until[fingerprint] = current + dt.timedelta(seconds=self.cooldown_seconds)
            return cached
        self._failed_until[fingerprint] = current + dt.timedelta(seconds=self.cooldown_seconds)
        status = (
            "normalization_empty"
            if saw_normalization_empty
            else "empty_payload"
            if saw_empty
            else "coverage_below_threshold"
            if saw_low_coverage
            else "provider_failed"
        )
        return self._failure_result(status=status, errors=errors, attempts=len(self.provider_names), now=current)
