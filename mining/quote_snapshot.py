from __future__ import annotations

import datetime as dt
import inspect
import json
import math
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Iterable

import pandas as pd

from .intraday import _call_endpoint_with_timeout, _call_tencent_quotes_with_timeout, normalize_spot_frame
from .selection_context import BENCHMARK_CODES


DEFAULT_PROVIDERS = ("tencent_batch", "stock_zh_a_spot_em")
DEFAULT_BENCHMARK_PROVIDERS = ("tencent_index", "stock_zh_index_spot_em")
PRIMARY_BENCHMARK = "000852"
TENCENT_BENCHMARK_CODES = ("sh000852", "sh000300", "sh000001", "sz399001")
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
    benchmark_status: str = "unavailable"
    benchmark_provider: str | None = None
    benchmark_errors: tuple[str, ...] = ()
    benchmark_observed_at: str | None = None


ProviderFetcher = Callable[..., pd.DataFrame]
BenchmarkFetcher = Callable[[str, float], pd.DataFrame]


def _as_china_time(value: dt.datetime) -> dt.datetime:
    return value.replace(tzinfo=CHINA_TZ) if value.tzinfo is None else value.astimezone(CHINA_TZ)


def _default_provider_fetcher(
    provider: str,
    timeout_seconds: float,
    expected_codes: set[str],
) -> pd.DataFrame:
    if provider == "tencent_batch":
        return _call_tencent_quotes_with_timeout(tuple(sorted(expected_codes)), timeout_seconds)
    return _call_endpoint_with_timeout(provider, timeout_seconds)


def _default_benchmark_fetcher(provider: str, timeout_seconds: float) -> pd.DataFrame:
    if provider == "tencent_index":
        return _call_tencent_quotes_with_timeout(TENCENT_BENCHMARK_CODES, timeout_seconds, prefix=True)
    return _call_endpoint_with_timeout(provider, timeout_seconds)


def _canonical_codes(values: Iterable[object]) -> set[str]:
    return {str(value).zfill(6) for value in values if str(value).strip()}


def _finite_positive_benchmarks(values: dict[str, float] | None) -> dict[str, float]:
    normalized: dict[str, float] = {}
    for raw_code, raw_close in (values or {}).items():
        try:
            close = float(raw_close)
        except (TypeError, ValueError):
            continue
        if math.isfinite(close) and close > 0:
            normalized[str(raw_code).zfill(6)] = close
    return dict(sorted(normalized.items()))


def _benchmark_column(frame: pd.DataFrame, names: tuple[str, ...]) -> str | None:
    lowered = {str(column).strip().lower(): str(column) for column in frame.columns}
    for name in names:
        if name in frame.columns:
            return name
        matched = lowered.get(name.lower())
        if matched is not None:
            return matched
    return None


def _normalize_benchmark_frame(frame: pd.DataFrame) -> dict[str, float]:
    if frame is None or frame.empty:
        return {}
    code_column = _benchmark_column(frame, ("代码", "指数代码", "code", "symbol", "sec_code"))
    close_column = _benchmark_column(frame, ("最新价", "最新", "现价", "close", "收盘"))
    if code_column is None or close_column is None:
        return {}
    required = set(BENCHMARK_CODES)
    values: dict[str, float] = {}
    for raw_code, raw_close in zip(frame[code_column], frame[close_column]):
        digits = "".join(character for character in str(raw_code or "") if character.isdigit())
        code = digits[-6:].zfill(6) if digits else ""
        if code not in required:
            continue
        try:
            close = float(raw_close)
        except (TypeError, ValueError):
            continue
        if math.isfinite(close) and close > 0:
            values[code] = close
    return dict(sorted(values.items()))


class QuoteSnapshotAdapter:
    """One bounded quote path with explicit failover, cooling, and restart cache rules."""

    def __init__(
        self,
        *,
        cache_dir: str | Path,
        provider_names: tuple[str, ...] = DEFAULT_PROVIDERS,
        provider_fetcher: ProviderFetcher = _default_provider_fetcher,
        benchmark_provider_names: tuple[str, ...] = DEFAULT_BENCHMARK_PROVIDERS,
        benchmark_fetcher: BenchmarkFetcher | None = None,
        timeout_seconds: float = 8.0,
        cooldown_seconds: float = 60.0,
        max_age_minutes: int = 10,
        min_coverage_ratio: float = 0.80,
        minimum_expected_codes: int = 1,
    ) -> None:
        if not provider_names:
            raise ValueError("provider_names must not be empty")
        if not 5.0 <= float(timeout_seconds) <= 8.0:
            raise ValueError("timeout_seconds must be between 5 and 8 seconds")
        self.cache_dir = Path(cache_dir)
        self.provider_names = tuple(provider_names)
        self.provider_fetcher = provider_fetcher
        self.benchmark_provider_names = tuple(benchmark_provider_names)
        self.benchmark_fetcher = (
            _default_benchmark_fetcher
            if benchmark_fetcher is None and provider_fetcher is _default_provider_fetcher
            else benchmark_fetcher
        )
        self.timeout_seconds = float(timeout_seconds)
        self.cooldown_seconds = float(cooldown_seconds)
        self.max_age_minutes = int(max_age_minutes)
        self.min_coverage_ratio = float(min_coverage_ratio)
        self.minimum_expected_codes = int(minimum_expected_codes)
        if self.minimum_expected_codes <= 0:
            raise ValueError("minimum_expected_codes must be positive")
        try:
            inspect.signature(provider_fetcher).bind("provider", 5.0, set())
            self._provider_fetcher_accepts_expected_codes = True
        except (TypeError, ValueError):
            self._provider_fetcher_accepts_expected_codes = False
        self._failed_until: dict[tuple[str, tuple[str, ...]], dt.datetime] = {}

    def _fetch_stock_provider(self, provider: str, expected_codes: set[str]) -> pd.DataFrame:
        if self._provider_fetcher_accepts_expected_codes:
            return pd.DataFrame(self.provider_fetcher(provider, self.timeout_seconds, expected_codes))
        return pd.DataFrame(self.provider_fetcher(provider, self.timeout_seconds))

    def _cache_paths(self, trade_date: str) -> tuple[Path, Path]:
        root = self.cache_dir / str(trade_date)
        return root / "snapshot.pkl", root / "metadata.json"

    def _bundle_path(self, trade_date: str) -> Path:
        return self.cache_dir / str(trade_date) / "snapshot_v2.pkl"

    def _coverage(self, frame: pd.DataFrame, expected_codes: set[str]) -> float:
        if not expected_codes or "sec_code" not in frame.columns:
            return 0.0
        observed = _canonical_codes(frame["sec_code"])
        return len(observed.intersection(expected_codes)) / len(expected_codes)

    def _save_success(self, trade_date: str, result: QuoteSnapshotResult) -> None:
        bundle_path = self._bundle_path(trade_date)
        bundle_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = bundle_path.with_suffix(".tmp.pkl")
        pd.to_pickle(
            {
                "schema_version": 2,
                "trade_date": str(trade_date),
                "provider": result.provider,
                "observed_at": result.observed_at,
                "raw_rows": result.raw_rows,
                "normalized_rows": result.normalized_rows,
                "coverage": result.coverage,
                "benchmark_closes": _finite_positive_benchmarks(result.benchmark_closes),
                "benchmark_status": result.benchmark_status,
                "benchmark_provider": result.benchmark_provider,
                "benchmark_errors": list(result.benchmark_errors),
                "benchmark_observed_at": result.benchmark_observed_at,
                "frame": result.frame.copy(),
            },
            temporary,
        )
        temporary.replace(bundle_path)

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
        bundle_path = self._bundle_path(trade_date)
        frame_path, metadata_path = self._cache_paths(trade_date)
        if not bundle_path.exists() and (not frame_path.exists() or not metadata_path.exists()):
            return None
        try:
            if bundle_path.exists():
                bundle = pd.read_pickle(bundle_path)
                if not isinstance(bundle, dict) or bundle.get("schema_version") != 2:
                    raise ValueError("unsupported snapshot bundle schema")
                metadata = bundle
                frame = pd.DataFrame(bundle["frame"]).copy()
                benchmark_closes = _finite_positive_benchmarks(bundle.get("benchmark_closes"))
                cache_errors: tuple[str, ...] = () if benchmark_closes else ("cached snapshot has no benchmark_closes",)
            else:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                frame = pd.read_pickle(frame_path)
                benchmark_closes = _finite_positive_benchmarks(metadata.get("benchmark_closes"))
                cache_errors = ("legacy cache has no benchmark_closes",) if not benchmark_closes else ()
            benchmark_status = "ready" if PRIMARY_BENCHMARK in benchmark_closes else "unavailable"
            benchmark_errors = tuple(str(item) for item in (metadata.get("benchmark_errors") or ()))
            if str(metadata.get("trade_date")) != str(trade_date):
                return None
            observed_at = dt.datetime.fromisoformat(str(metadata["observed_at"]))
            observed_at = _as_china_time(observed_at)
            age_minutes = (now - observed_at).total_seconds() / 60.0
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
                errors=cache_errors,
                status="snapshot_cache_fallback",
                from_cache=True,
                benchmark_closes=benchmark_closes,
                benchmark_status=benchmark_status,
                benchmark_provider=metadata.get("benchmark_provider"),
                benchmark_errors=benchmark_errors,
                benchmark_observed_at=metadata.get("benchmark_observed_at") or metadata.get("observed_at"),
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

    def _fetch_benchmarks(
        self,
        now: dt.datetime,
    ) -> tuple[dict[str, float], str, str | None, tuple[str, ...], str]:
        observed_at = now.isoformat()
        if self.benchmark_fetcher is None:
            return {}, "unavailable", None, ("benchmark_fetcher_not_configured",), observed_at
        errors: list[str] = []
        partial: tuple[dict[str, float], str] | None = None
        for provider in self.benchmark_provider_names:
            try:
                frame = pd.DataFrame(self.benchmark_fetcher(provider, self.timeout_seconds))
                closes = _normalize_benchmark_frame(frame)
                if closes and partial is None:
                    partial = (closes, provider)
                if PRIMARY_BENCHMARK not in closes:
                    errors.append(f"{provider}:missing_or_invalid_primary:{PRIMARY_BENCHMARK}")
                    continue
                missing = sorted(set(BENCHMARK_CODES).difference(closes))
                if missing:
                    errors.append(f"{provider}:missing_required:{','.join(missing)}")
                return closes, "ready", provider, tuple(errors), observed_at
            except Exception as exc:
                errors.append(f"{provider}:{type(exc).__name__}:{exc}")
        if partial is not None:
            return partial[0], "unavailable", partial[1], tuple(errors), observed_at
        return {}, "unavailable", None, tuple(errors), observed_at

    def load(
        self,
        trade_date: str,
        *,
        expected_codes: Iterable[object],
        now: dt.datetime,
        close_final: bool = False,
        cache_only: bool = False,
    ) -> QuoteSnapshotResult:
        current = _as_china_time(now)
        expected = _canonical_codes(expected_codes)
        if len(expected) < self.minimum_expected_codes:
            return self._failure_result(
                status="invalid_coverage_denominator",
                errors=[
                    f"expected_codes_count={len(expected)} below minimum={self.minimum_expected_codes}"
                ],
                attempts=0,
                now=current,
            )
        if cache_only:
            cached = self._cached_result(str(trade_date), expected, current, close_final=close_final)
            if cached is not None:
                return cached
            return self._failure_result(
                status="provider_failed",
                errors=["cache_only snapshot unavailable"],
                attempts=0,
                now=current,
            )
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
        normalized_rows = 0
        reported_raw_rows = 0
        reported_normalized_rows = 0
        best_coverage = 0.0
        for attempt, provider in enumerate(self.provider_names, start=1):
            try:
                raw = self._fetch_stock_provider(provider, expected)
                raw_rows = len(raw)
                if raw.empty:
                    saw_empty = True
                    errors.append(f"{provider}:empty_payload")
                    continue
                normalized = normalize_spot_frame(raw)
                normalized_rows = len(normalized)
                if normalized.empty:
                    saw_normalization_empty = True
                    errors.append(f"{provider}:normalization_empty")
                    continue
                coverage = self._coverage(normalized, expected)
                if coverage >= best_coverage:
                    best_coverage = coverage
                    reported_raw_rows = raw_rows
                    reported_normalized_rows = normalized_rows
                if coverage < self.min_coverage_ratio:
                    saw_low_coverage = True
                    errors.append(f"{provider}:coverage_below_threshold:{coverage:.3f}")
                    continue
                benchmark_closes, benchmark_status, benchmark_provider, benchmark_errors, benchmark_observed_at = (
                    self._fetch_benchmarks(current)
                )
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
                    benchmark_closes=benchmark_closes,
                    benchmark_status=benchmark_status,
                    benchmark_provider=benchmark_provider,
                    benchmark_errors=benchmark_errors,
                    benchmark_observed_at=benchmark_observed_at,
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
        failed = self._failure_result(status=status, errors=errors, attempts=len(self.provider_names), now=current)
        return replace(
            failed,
            raw_rows=reported_raw_rows,
            normalized_rows=reported_normalized_rows,
            coverage=best_coverage,
        )
