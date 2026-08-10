from __future__ import annotations

import datetime as dt
import math
import multiprocessing
import os
import queue
import re
import statistics
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from .db import list_stock_trade_dates
from .scanners.launch_burst import LaunchBurstScanner, select_candidates_from_history


A_SHARE_CODE = re.compile(r"^(600|601|603|605|688|000|001|002|003|300|301)\d{3}$")
SPOT_COLUMNS = ["sec_type", "sec_code", "sec_name", "trade_date", "open", "high", "low", "close", "pre_close", "volume", "amount"]
VOLUME_RATIO_SAME_UNIT = (0.5, 3.0)
VOLUME_RATIO_LOT_UNIT = (50.0, 300.0)
FROZEN_VOLUME_TOLERANCE = 0.005
FROZEN_MATCH_THRESHOLD = 0.8
DB_LAG_OVERRIDE_ENV = "INTRADAY_ALLOW_DB_LAG"


def _pick_column(frame: pd.DataFrame, names: tuple[str, ...]) -> str | None:
    lowered = {str(column).strip().lower(): str(column) for column in frame.columns}
    for name in names:
        if name in frame.columns:
            return name
        matched = lowered.get(name.strip().lower())
        if matched:
            return matched
    return None


def _canonical_code(value: object) -> str:
    digits = "".join(character for character in str(value or "") if character.isdigit())
    return digits[-6:].zfill(6) if digits else ""


def _scale_amount_to_yuan(series: pd.Series) -> pd.Series:
    amount = pd.to_numeric(series, errors="coerce")
    median = float(amount.dropna().median()) if not amount.dropna().empty else math.nan
    if math.isfinite(median) and median < 100_000_000:
        return amount * 10_000.0
    return amount


def _median_implied_volume_ratio(frame: pd.DataFrame) -> float:
    valid = frame[
        frame["close"].gt(0)
        & frame["volume"].gt(0)
        & frame["amount"].gt(0)
    ]
    ratios = (valid["amount"] / valid["close"] / valid["volume"]).replace([math.inf, -math.inf], math.nan).dropna()
    if ratios.empty:
        return math.nan
    return float(ratios.median())


def _normalize_volume_unit(frame: pd.DataFrame) -> pd.DataFrame:
    ratio = _median_implied_volume_ratio(frame)
    if not math.isfinite(ratio):
        raise RuntimeError("A-share spot volume unit check failed: no comparable volume/amount rows")
    if VOLUME_RATIO_SAME_UNIT[0] <= ratio <= VOLUME_RATIO_SAME_UNIT[1]:
        return frame
    if VOLUME_RATIO_LOT_UNIT[0] <= ratio <= VOLUME_RATIO_LOT_UNIT[1]:
        frame = frame.copy()
        frame["volume"] = frame["volume"] * 100.0
        return frame
    raise RuntimeError(f"A-share spot volume unit check failed: median implied volume ratio={ratio:.2f}")


def _previous_business_day(day: str) -> str:
    current = dt.date.fromisoformat(day)
    delta = 3 if current.weekday() == 0 else 1
    return (current - dt.timedelta(days=delta)).isoformat()


def _allow_db_lag() -> bool:
    return os.environ.get(DB_LAG_OVERRIDE_ENV) == "1"


def _validate_local_history_freshness(dates: list[str], today: str) -> tuple[str, bool]:
    before_today = [str(day) for day in dates if str(day) < today]
    if not before_today:
        raise RuntimeError("No local stock history before the intraday scan date is available.")
    baseline_date = before_today[-1]
    latest_date = str(dates[-1])
    expected = _previous_business_day(today)
    allow_lag = _allow_db_lag()
    if latest_date < expected and not allow_lag:
        raise RuntimeError(
            "Local stock history is stale for intraday scanning: "
            f"latest={latest_date}, expected_at_least={expected}. "
            "Run offline_daily_update.py before scanning, or set INTRADAY_ALLOW_DB_LAG=1 after manual confirmation."
        )
    return baseline_date, bool(latest_date < expected and allow_lag)


def _reject_frozen_snapshot(history: pd.DataFrame, spot: pd.DataFrame, baseline_date: str) -> None:
    baseline = history[history["trade_date"].astype(str).eq(str(baseline_date))].copy()
    if baseline.empty:
        return
    baseline["sec_code"] = baseline["sec_code"].astype(str).str.zfill(6)
    current = spot.copy()
    current["sec_code"] = current["sec_code"].astype(str).str.zfill(6)
    joined = current[["sec_code", "close", "volume"]].merge(
        baseline[["sec_code", "close", "volume"]],
        on="sec_code",
        suffixes=("_spot", "_history"),
        how="inner",
    )
    if joined.empty:
        return
    close_match = (joined["close_spot"] - joined["close_history"]).abs().le(1e-9)
    volume_base = joined["volume_history"].abs().where(joined["volume_history"].abs().gt(0), math.nan)
    volume_match = ((joined["volume_spot"] - joined["volume_history"]).abs() / volume_base).le(FROZEN_VOLUME_TOLERANCE)
    frozen = close_match & volume_match.fillna(False)
    share = float(frozen.mean())
    if share >= FROZEN_MATCH_THRESHOLD:
        raise RuntimeError(
            "Intraday frozen spot snapshot detected: "
            f"{int(frozen.sum())}/{len(joined)} rows match local baseline {baseline_date}. "
            "The market data source may be returning a holiday or stale snapshot."
        )


def normalize_spot_frame(frame: pd.DataFrame) -> pd.DataFrame:
    if frame is None or frame.empty:
        raise RuntimeError("A-share spot snapshot is empty.")
    source_attrs = dict(frame.attrs)

    columns = {
        "sec_code": _pick_column(frame, ("代码", "证券代码", "股票代码", "code", "symbol", "sec_code")),
        "sec_name": _pick_column(frame, ("名称", "股票名称", "name", "sec_name")),
        "close": _pick_column(frame, ("最新价", "最新", "现价", "price", "last", "close", "收盘")),
        "pre_close": _pick_column(frame, ("昨收", "昨收价", "pre_close", "preClose", "previous_close")),
        "open": _pick_column(frame, ("今开", "开盘", "open")),
        "high": _pick_column(frame, ("最高", "high")),
        "low": _pick_column(frame, ("最低", "low")),
        "volume": _pick_column(frame, ("成交量", "volume", "vol", "成交量(手)")),
        "amount": _pick_column(frame, ("amount_yuan", "成交额", "成交额(元)", "成交额(万元)", "成交额(万)", "amount", "turnover", "成交金额")),
    }
    required = ("sec_code", "close", "open", "high", "low", "volume", "amount")
    missing = [name for name in required if columns[name] is None]
    if missing:
        raise RuntimeError(f"A-share spot snapshot missing columns: {','.join(missing)}")

    result = pd.DataFrame()
    result["sec_code"] = frame[columns["sec_code"]].map(_canonical_code)
    result["sec_name"] = (
        frame[columns["sec_name"]].astype(str).str.strip()
        if columns["sec_name"] is not None
        else result["sec_code"]
    )
    for name in ("close", "pre_close", "open", "high", "low", "volume"):
        result[name] = (
            pd.to_numeric(frame[columns[name]], errors="coerce")
            if columns[name] is not None
            else math.nan
        )
    result["amount"] = (
        pd.to_numeric(frame[columns["amount"]], errors="coerce")
        if columns["amount"] == "amount_yuan"
        else _scale_amount_to_yuan(frame[columns["amount"]])
    )
    result = result[result["sec_code"].map(lambda code: bool(A_SHARE_CODE.match(str(code))))]
    result = result.dropna(subset=["close", "open", "high", "low", "volume", "amount"])
    result = result[
        result["close"].gt(0)
        & result["open"].gt(0)
        & result["high"].gt(0)
        & result["volume"].gt(0)
        & result["amount"].gt(0)
    ]
    result = _normalize_volume_unit(result)
    result["sec_type"] = "stock"
    normalized = result.drop_duplicates("sec_code", keep="last").reset_index(drop=True)
    normalized.attrs.update(source_attrs)
    return normalized


def _tencent_quotes_to_frame(payload: dict[str, dict[str, Any]]) -> pd.DataFrame:
    """Map pqquotation's Tencent fields to the existing raw spot contract."""
    rows = []
    for key, record in (payload or {}).items():
        rows.append(
            {
                "sec_code": record.get("code") or key,
                "sec_name": record.get("name"),
                "close": record.get("now"),
                "pre_close": record.get("close"),
                "open": record.get("open"),
                "high": record.get("high"),
                "low": record.get("low"),
                "volume": record.get("volume"),
                "amount_yuan": record.get("amount", record.get("成交额(万)")),
                "provider_timestamp": record.get("datetime"),
            }
        )
    return pd.DataFrame(rows)


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = (len(ordered) - 1) * fraction
    lower = math.floor(index)
    upper = math.ceil(index)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower)


def _pq_tencent_batch_fetcher(
    stock_codes: tuple[str, ...],
    timeout_seconds: float,
    prefix: bool,
) -> dict[str, dict[str, Any]]:
    import pqquotation

    quotation = pqquotation.use("tencent")
    quotation.timeout = float(timeout_seconds)
    return quotation.real(
        list(stock_codes),
        return_format="prefix" if prefix else "digit",
    )


def _fetch_tencent_batches(
    stock_codes: tuple[str, ...],
    *,
    batch_fetcher: Callable[[tuple[str, ...], float, bool], object] = _pq_tencent_batch_fetcher,
    max_workers: int = 8,
    request_timeout_seconds: float = 2.0,
    collection_budget_seconds: float = 6.5,
    prefix: bool = False,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Fetch explicit Tencent batches while retaining completed partial results."""
    if max_workers <= 0 or request_timeout_seconds <= 0 or collection_budget_seconds <= 0:
        raise ValueError("Tencent batch limits must be positive")
    requested = tuple(sorted({str(code).strip() for code in stock_codes if str(code).strip()}))
    batches = [requested[index : index + 60] for index in range(0, len(requested), 60)]
    if not batches:
        raise ValueError("Tencent quote request requires explicit stock codes")

    collection_started = time.monotonic()
    deadline = collection_started + float(collection_budget_seconds)
    lock = threading.Lock()
    active = 0
    max_concurrency = 0

    def run_batch(batch_index: int, batch: tuple[str, ...]) -> tuple[dict[str, Any], pd.DataFrame]:
        nonlocal active, max_concurrency
        started = time.monotonic()
        with lock:
            active += 1
            max_concurrency = max(max_concurrency, active)
        try:
            payload = batch_fetcher(batch, float(request_timeout_seconds), bool(prefix))
            frame = pd.DataFrame(payload) if isinstance(payload, pd.DataFrame) else _tencent_quotes_to_frame(payload)
            record = {
                "batch": batch_index,
                "requested": len(batch),
                "status": "success",
                "elapsed_seconds": round(time.monotonic() - started, 6),
                "rows": len(frame),
                "error": None,
            }
            return record, frame
        except TimeoutError as exc:
            return {
                "batch": batch_index,
                "requested": len(batch),
                "status": "timeout",
                "elapsed_seconds": round(time.monotonic() - started, 6),
                "rows": 0,
                "error": f"{type(exc).__name__}:{exc}",
            }, pd.DataFrame()
        except Exception as exc:
            return {
                "batch": batch_index,
                "requested": len(batch),
                "status": "error",
                "elapsed_seconds": round(time.monotonic() - started, 6),
                "rows": 0,
                "error": f"{type(exc).__name__}:{exc}",
            }, pd.DataFrame()
        finally:
            with lock:
                active -= 1

    executor = ThreadPoolExecutor(max_workers=min(int(max_workers), len(batches)))
    pending: dict[object, tuple[int, tuple[str, ...]]] = {}
    next_batch = 0
    records: dict[int, dict[str, Any]] = {}
    frames: list[pd.DataFrame] = []

    def submit_available(*, initial: bool = False) -> None:
        nonlocal next_batch
        while next_batch < len(batches) and len(pending) < max_workers:
            if not initial and time.monotonic() + request_timeout_seconds > deadline:
                break
            batch_index = next_batch
            batch = batches[batch_index]
            pending[executor.submit(run_batch, batch_index, batch)] = (batch_index, batch)
            next_batch += 1

    submit_available(initial=True)
    try:
        while pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            completed, _ = wait(tuple(pending), timeout=remaining, return_when=FIRST_COMPLETED)
            if not completed:
                break
            for future in completed:
                batch_index, _ = pending.pop(future)
                record, frame = future.result()
                records[batch_index] = record
                if not frame.empty:
                    frames.append(frame)
            submit_available()
    finally:
        timed_out = list(pending.values()) + [
            (batch_index, batches[batch_index])
            for batch_index in range(next_batch, len(batches))
        ]
        for future in pending:
            future.cancel()
        executor.shutdown(wait=False, cancel_futures=True)
        for batch_index, batch in timed_out:
            records.setdefault(
                batch_index,
                {
                    "batch": batch_index,
                    "requested": len(batch),
                    "status": "timeout",
                    "elapsed_seconds": round(float(collection_budget_seconds), 6),
                    "rows": 0,
                    "error": "collection_deadline",
                },
            )

    merged = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    if not merged.empty and "sec_code" in merged.columns:
        merged["sec_code"] = merged["sec_code"].map(_canonical_code)
        if not prefix:
            merged = merged[merged["sec_code"].map(lambda code: bool(A_SHARE_CODE.match(str(code))))]
        merged = merged.drop_duplicates("sec_code", keep="last").sort_values("sec_code").reset_index(drop=True)

    requested_canonical = {_canonical_code(code) for code in requested}
    received = set(merged["sec_code"].astype(str)) if "sec_code" in merged.columns else set()
    timestamps = (
        pd.to_datetime(merged["provider_timestamp"], errors="coerce").dropna()
        if "provider_timestamp" in merged.columns
        else pd.Series(dtype="datetime64[ns]")
    )
    ordered_records = [records[index] for index in range(len(batches))]
    latencies = [float(record["elapsed_seconds"]) for record in ordered_records]
    date_counts = (
        {
            str(day): int(count)
            for day, count in timestamps.dt.strftime("%Y-%m-%d").value_counts().sort_index().items()
        }
        if not timestamps.empty
        else {}
    )
    diagnostics = {
        "collection_elapsed_seconds": round(time.monotonic() - collection_started, 6),
        "total_batches": len(batches),
        "success_batches": sum(record["status"] == "success" for record in ordered_records),
        "timeout_batches": sum(record["status"] == "timeout" for record in ordered_records),
        "error_batches": sum(record["status"] == "error" for record in ordered_records),
        "requested_codes": len(requested_canonical),
        "received_codes": len(received.intersection(requested_canonical)),
        "missing_codes": len(requested_canonical.difference(received)),
        "max_workers": int(max_workers),
        "max_concurrency": max_concurrency,
        "latency_p50_seconds": round(statistics.median(latencies), 6) if latencies else 0.0,
        "latency_p95_seconds": round(_percentile(latencies, 0.95), 6),
        "latency_max_seconds": round(max(latencies), 6) if latencies else 0.0,
        "provider_timestamp_min": timestamps.min().isoformat() if not timestamps.empty else None,
        "provider_timestamp_max": timestamps.max().isoformat() if not timestamps.empty else None,
        "provider_date_counts": date_counts,
        "batches": ordered_records,
    }
    merged.attrs["tencent_batch_diagnostics"] = diagnostics
    return merged, diagnostics


def _spot_worker(result_queue: Any, endpoint_name: str) -> None:
    try:
        import akshare as ak

        endpoint = getattr(ak, endpoint_name)
        result_queue.put(("ok", endpoint()))
    except Exception as exc:
        result_queue.put(("error", f"{type(exc).__name__}: {exc}"))


def _tencent_worker(
    result_queue: Any,
    stock_codes: tuple[str, ...],
    prefix: bool,
    max_workers: int,
    request_timeout_seconds: float,
    collection_budget_seconds: float,
) -> None:
    try:
        frame, _ = _fetch_tencent_batches(
            stock_codes,
            max_workers=max_workers,
            request_timeout_seconds=request_timeout_seconds,
            collection_budget_seconds=collection_budget_seconds,
            prefix=prefix,
        )
        result_queue.put(("ok", frame))
    except Exception as exc:
        result_queue.put(("error", f"{type(exc).__name__}: {exc}"))


def _call_frame_worker_with_timeout(
    worker: Callable[..., None],
    worker_args: tuple[Any, ...],
    label: str,
    timeout_seconds: float,
) -> pd.DataFrame:
    context = multiprocessing.get_context("spawn")
    result_queue = context.Queue(maxsize=1)
    process = context.Process(target=worker, args=(result_queue, *worker_args))
    process.start()
    deadline = time.monotonic() + timeout_seconds
    status = None
    payload: object = None
    try:
        while time.monotonic() < deadline:
            try:
                status, payload = result_queue.get(timeout=min(0.25, max(deadline - time.monotonic(), 0.01)))
                break
            except queue.Empty:
                if not process.is_alive():
                    break
        if status is None:
            if process.is_alive():
                process.terminate()
            raise TimeoutError(f"{label} timed out after {timeout_seconds:.1f}s")
        if status != "ok":
            raise RuntimeError(f"{label} failed: {payload}")
        if not isinstance(payload, pd.DataFrame):
            raise RuntimeError(f"{label} returned an invalid payload")
        return payload
    finally:
        if process.is_alive():
            process.terminate()
        process.join(timeout=1.0)
        result_queue.close()


def _call_endpoint_with_timeout(endpoint_name: str, timeout_seconds: float) -> pd.DataFrame:
    return _call_frame_worker_with_timeout(
        _spot_worker,
        (endpoint_name,),
        endpoint_name,
        timeout_seconds,
    )


def _call_tencent_quotes_with_timeout(
    stock_codes: tuple[str, ...],
    timeout_seconds: float,
    *,
    prefix: bool = False,
    max_workers: int = 8,
    request_timeout_seconds: float = 2.0,
    collection_budget_seconds: float = 6.5,
) -> pd.DataFrame:
    if not stock_codes:
        raise ValueError("Tencent quote request requires explicit stock codes")
    if collection_budget_seconds > timeout_seconds - 1.0:
        raise ValueError("Tencent collection budget must leave at least 1 second for the parent process")
    return _call_frame_worker_with_timeout(
        _tencent_worker,
        (
            tuple(stock_codes),
            bool(prefix),
            int(max_workers),
            float(request_timeout_seconds),
            float(collection_budget_seconds),
        ),
        "tencent_batch",
        timeout_seconds,
    )


def fetch_spot_frame(
    timeout_seconds: float = 20.0,
    retries: int = 3,
    min_rows: int = 1000,
) -> pd.DataFrame:
    if timeout_seconds <= 0 or retries <= 0 or min_rows <= 0:
        raise ValueError("timeout_seconds, retries, and min_rows must be positive")
    failures = []
    for endpoint_name in ("stock_zh_a_spot_em", "stock_zh_a_spot"):
        for attempt in range(1, retries + 1):
            try:
                normalized = normalize_spot_frame(
                    _call_endpoint_with_timeout(endpoint_name, timeout_seconds)
                )
                if len(normalized) < min_rows:
                    raise RuntimeError(
                        f"{endpoint_name} returned only {len(normalized)} valid A-share rows"
                    )
                return normalized
            except Exception as exc:
                failures.append(f"{endpoint_name} attempt {attempt}: {exc}")
    raise RuntimeError("All A-share spot endpoints failed: " + " | ".join(failures))


def build_synthetic_history(
    conn: Any,
    spot_df: pd.DataFrame,
    today: str,
    history_days: int = 90,
) -> pd.DataFrame:
    if history_days <= 0:
        raise ValueError("history_days must be positive")
    missing = [
        column
        for column in SPOT_COLUMNS
        if column not in ("sec_type", "trade_date") and column not in spot_df.columns
    ]
    if missing:
        raise ValueError(f"Normalized spot snapshot missing columns: {','.join(missing)}")
    spot = spot_df.copy()
    spot["sec_type"] = "stock"
    dates = list_stock_trade_dates(conn, end_date=today, limit=history_days, include_end=True)
    if not dates:
        raise RuntimeError("No local stock history is available for intraday scanning.")
    baseline_date, db_lag_allowed = _validate_local_history_freshness(dates, today)
    placeholders = ",".join("?" for _ in dates)
    history = pd.read_sql_query(
        f"""
        SELECT sec_type, sec_code, trade_date, open, high, low, close,
               pre_close, volume, amount
        FROM ash.kline_daily
        WHERE sec_type='stock'
          AND trade_date IN ({placeholders})
        ORDER BY sec_code, trade_date
        """,
        conn,
        params=dates,
    )
    try:
        names = pd.read_sql_query("SELECT sec_code, name AS sec_name FROM ash.stock_info", conn)
    except Exception:
        names = pd.DataFrame(columns=["sec_code", "sec_name"])
    if not names.empty:
        names["sec_code"] = names["sec_code"].astype(str).str.zfill(6)
        history["sec_code"] = history["sec_code"].astype(str).str.zfill(6)
        history = history.merge(names.drop_duplicates("sec_code", keep="last"), on="sec_code", how="left")
    else:
        history["sec_name"] = history["sec_code"]
    history["sec_name"] = history["sec_name"].fillna(history["sec_code"])

    _reject_frozen_snapshot(history, spot, baseline_date)
    history = history[history["trade_date"].astype(str).ne(today)].copy()
    spot["trade_date"] = today
    spot = spot[SPOT_COLUMNS]
    combined = pd.concat([history[SPOT_COLUMNS], spot], ignore_index=True)
    combined = combined.drop_duplicates(["sec_type", "sec_code", "trade_date"], keep="last").reset_index(drop=True)
    combined.attrs["intraday_baseline_date"] = baseline_date
    combined.attrs["intraday_db_lag_allowed"] = db_lag_allowed
    return combined


def _china_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=8)))


def _as_china_time(current: dt.datetime) -> dt.datetime:
    china_tz = dt.timezone(dt.timedelta(hours=8))
    if current.tzinfo is None:
        return current.replace(tzinfo=china_tz)
    return current.astimezone(china_tz)


def _validate_scan_window(current: dt.datetime, window_start: dt.time, window_end: dt.time) -> None:
    if window_start > window_end:
        raise ValueError("window_start must not be later than window_end")
    current = _as_china_time(current)
    if current.weekday() >= 5 or not (window_start <= current.time().replace(tzinfo=None) <= window_end):
        raise RuntimeError(
            f"Intraday launch scan is only allowed on weekdays between "
            f"{window_start.strftime('%H:%M')} and {window_end.strftime('%H:%M')} China time."
        )


def candidates_to_frame(candidates: list[Any]) -> pd.DataFrame:
    rows = []
    for candidate in candidates:
        rows.append(
            {
                "rank": candidate.rank,
                "sec_code": candidate.sec_code,
                "sec_name": candidate.sec_name,
                "pct": candidate.features.get("pct"),
                "volume_ratio": candidate.features.get("volume_ratio"),
                "sigma_multiple": candidate.features.get("sigma_multiple"),
                "cluster_days": candidate.features.get("cluster_days"),
            }
        )
    return pd.DataFrame(rows, columns=["rank", "sec_code", "sec_name", "pct", "volume_ratio", "sigma_multiple", "cluster_days"])


def _frame_to_markdown(frame: pd.DataFrame) -> str:
    def cell(value: object) -> str:
        return str(value).replace("|", "\\|").replace("\n", " ")

    headers = [cell(column) for column in frame.columns]
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    lines.extend(
        "| " + " | ".join(cell(value) for value in row) + " |"
        for row in frame.itertuples(index=False, name=None)
    )
    return "\n".join(lines)


def _write_intraday_report(
    display: pd.DataFrame,
    current: dt.datetime,
    out_dir: str | Path,
    baseline_date: str | None = None,
    db_lag_allowed: bool = False,
) -> str:
    output = Path(out_dir)
    output.mkdir(parents=True, exist_ok=True)
    path = output / f"launch_intraday_{current.strftime('%Y%m%d_%H%M')}.md"
    report = display.copy()
    if not report.empty:
        report["pct"] = report["pct"].map(lambda value: f"{float(value) * 100:.1f}%")
        report["volume_ratio"] = report["volume_ratio"].map(lambda value: f"{float(value):.2f}")
        report["sigma_multiple"] = report["sigma_multiple"].map(lambda value: f"{float(value):.2f}")
        table = _frame_to_markdown(report)
    else:
        table = "�޺�ѡ��"
    lines = [
        f"# ������������ {current.strftime('%Y-%m-%d %H:%M')}",
        f"基线截止 T-1={baseline_date or 'n/a'}",
    ]
    if db_lag_allowed:
        lines.append(f"DB lag override: {DB_LAG_OVERRIDE_ENV}=1")
    lines.extend(
        [
            "",
            "�ھ���ʵʱ�ۼ�������ʱ��������ƣ�����������й۲죬��д�����ߺ�ѡ��ǰ��������",
            "",
            table,
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")
    return str(path)


def scan_intraday(
    conn: Any,
    params_override: dict[str, Any] | None = None,
    *,
    now: dt.datetime | None = None,
    spot_df: pd.DataFrame | None = None,
    out_dir: str | Path = "output/intraday",
    window_start: dt.time = dt.time(14, 0),
    window_end: dt.time = dt.time(15, 0),
) -> dict[str, Any]:
    current = _as_china_time(now or _china_now())
    _validate_scan_window(current, window_start, window_end)
    today = current.date().isoformat()
    spot = fetch_spot_frame() if spot_df is None else normalize_spot_frame(spot_df)
    scanner = LaunchBurstScanner(params=params_override)
    history = build_synthetic_history(
        conn,
        spot,
        today,
        history_days=int(scanner.params["history_days"]),
    )
    candidates = select_candidates_from_history(
        history,
        today,
        scanner.params,
        scanner.strategy_id,
        scanner.version,
    )
    display = candidates_to_frame(candidates)
    baseline_date = history.attrs.get("intraday_baseline_date")
    db_lag_allowed = bool(history.attrs.get("intraday_db_lag_allowed"))
    report = _write_intraday_report(display, current, out_dir, baseline_date, db_lag_allowed)
    return {
        "trade_date": today,
        "as_of": current.isoformat(),
        "baseline_date": baseline_date,
        "db_lag_allowed": db_lag_allowed,
        "candidates": candidates,
        "display": display,
        "report": report,
    }
