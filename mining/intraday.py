from __future__ import annotations

import datetime as dt
import math
import multiprocessing
import queue
import re
import time
from pathlib import Path
from typing import Any

import pandas as pd

from .db import list_stock_trade_dates
from .scanners.launch_burst import LaunchBurstScanner, select_candidates_from_history


A_SHARE_CODE = re.compile(r"^(600|601|603|605|688|000|001|002|003|300|301)\d{3}$")
SPOT_COLUMNS = ["sec_type", "sec_code", "sec_name", "trade_date", "open", "high", "low", "close", "pre_close", "volume", "amount"]


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


def normalize_spot_frame(frame: pd.DataFrame) -> pd.DataFrame:
    if frame is None or frame.empty:
        raise RuntimeError("A-share spot snapshot is empty.")

    columns = {
        "sec_code": _pick_column(frame, ("代码", "证券代码", "股票代码", "code", "symbol", "sec_code")),
        "sec_name": _pick_column(frame, ("名称", "股票名称", "name", "sec_name")),
        "close": _pick_column(frame, ("最新价", "最新", "现价", "price", "last", "close", "收盘")),
        "pre_close": _pick_column(frame, ("昨收", "昨收价", "pre_close", "preClose", "previous_close")),
        "open": _pick_column(frame, ("今开", "开盘", "open")),
        "high": _pick_column(frame, ("最高", "high")),
        "low": _pick_column(frame, ("最低", "low")),
        "volume": _pick_column(frame, ("成交量", "volume", "vol", "成交量(手)")),
        "amount": _pick_column(frame, ("成交额", "成交额(元)", "成交额(万元)", "成交额(万)", "amount", "turnover", "成交金额")),
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
    result["amount"] = _scale_amount_to_yuan(frame[columns["amount"]])
    result = result[result["sec_code"].map(lambda code: bool(A_SHARE_CODE.match(str(code))))]
    result = result.dropna(subset=["close", "open", "high", "low", "volume", "amount"])
    result = result[
        result["close"].gt(0)
        & result["open"].gt(0)
        & result["high"].gt(0)
        & result["volume"].gt(0)
        & result["amount"].gt(0)
    ]
    result["sec_type"] = "stock"
    return result.drop_duplicates("sec_code", keep="last").reset_index(drop=True)


def _spot_worker(result_queue: Any, endpoint_name: str) -> None:
    try:
        import akshare as ak

        endpoint = getattr(ak, endpoint_name)
        result_queue.put(("ok", endpoint()))
    except Exception as exc:
        result_queue.put(("error", f"{type(exc).__name__}: {exc}"))


def _call_endpoint_with_timeout(endpoint_name: str, timeout_seconds: float) -> pd.DataFrame:
    context = multiprocessing.get_context("spawn")
    result_queue = context.Queue(maxsize=1)
    process = context.Process(target=_spot_worker, args=(result_queue, endpoint_name))
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
            raise TimeoutError(f"{endpoint_name} timed out after {timeout_seconds:.1f}s")
        if status != "ok":
            raise RuntimeError(f"{endpoint_name} failed: {payload}")
        if not isinstance(payload, pd.DataFrame):
            raise RuntimeError(f"{endpoint_name} returned an invalid payload")
        return payload
    finally:
        if process.is_alive():
            process.terminate()
        process.join(timeout=1.0)
        result_queue.close()


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

    history = history[history["trade_date"].astype(str).ne(today)].copy()
    spot["trade_date"] = today
    spot = spot[SPOT_COLUMNS]
    combined = pd.concat([history[SPOT_COLUMNS], spot], ignore_index=True)
    return combined.drop_duplicates(["sec_type", "sec_code", "trade_date"], keep="last").reset_index(drop=True)


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


def _write_intraday_report(display: pd.DataFrame, current: dt.datetime, out_dir: str | Path) -> str:
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
        table = "无候选。"
    lines = [
        f"# 盘中主升启动 {current.strftime('%Y-%m-%d %H:%M')}",
        "",
        "口径：实时累计量不做时间比例外推；结果仅供盘中观察，不写入日线候选与前向结果表。",
        "",
        table,
    ]
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
    report = _write_intraday_report(display, current, out_dir)
    return {
        "trade_date": today,
        "as_of": current.isoformat(),
        "candidates": candidates,
        "display": display,
        "report": report,
    }
