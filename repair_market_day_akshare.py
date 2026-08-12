from __future__ import annotations

import argparse
import importlib
import json
import multiprocessing as mp
import sqlite3
import threading
import time
from pathlib import Path
from queue import Empty
from typing import Any, Callable, Iterable

import pandas as pd

from mining.data_quality import ROW_VALID_TRADE, classify_stock_row, raw_market_postcondition


KLINE_COLS = [
    "sec_type",
    "sec_code",
    "trade_date",
    "open",
    "high",
    "low",
    "close",
    "pre_close",
    "change",
    "change_pct",
    "volume",
    "amount",
    "turnover_ratio",
    "source",
    "updated_at",
]

REQUIRED_INDEX_CODES = ("000001", "399001", "000300", "000852")
MAX_FALLBACK_CLOSE_JUMP = 0.30
PROVIDER_CALL_TIMEOUT_SEC = 12.0
DEFAULT_STOCK_WORKERS = 1
DEFAULT_STOCK_REQUEST_INTERVAL_SEC = 0.45
PROVIDER_ERROR_CIRCUIT_THRESHOLD = 3


class GlobalRequestPacer:
    """Space provider request starts across every worker in one repair run."""

    def __init__(self, min_interval_sec: float = 0.0) -> None:
        self._interval = max(0.0, float(min_interval_sec))
        self._lock = threading.Lock()
        self._next_start = 0.0

    def wait(self) -> None:
        if self._interval <= 0.0:
            return
        with self._lock:
            now = time.monotonic()
            delay = max(0.0, self._next_start - now)
            self._next_start = max(self._next_start, now) + self._interval
        if delay > 0.0:
            time.sleep(delay)


def now_iso() -> str:
    return pd.Timestamp.now().isoformat(timespec="seconds")


def market_prefix(code: str) -> str:
    code = "".join(ch for ch in str(code) if ch.isdigit())[-6:].zfill(6)
    if code.startswith(("000", "001", "002", "003", "300", "301")):
        return "sz"
    if code.startswith(("60", "68")):
        return "sh"
    if code.startswith(("8", "4", "9")):
        return "bj"
    return "sz"


def index_market_prefix(code: str) -> str:
    code = "".join(ch for ch in str(code) if ch.isdigit())[-6:].zfill(6)
    if code.startswith("399"):
        return "sz"
    return "sh"


def normalize_day(value: str) -> tuple[str, str]:
    ts = pd.Timestamp(str(value).replace("/", "-")[:10])
    return ts.strftime("%Y-%m-%d"), ts.strftime("%Y%m%d")


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS kline_daily (
          sec_type TEXT NOT NULL,
          sec_code TEXT NOT NULL,
          trade_date TEXT NOT NULL,
          open REAL,
          high REAL,
          low REAL,
          close REAL,
          pre_close REAL,
          change REAL,
          change_pct REAL,
          volume REAL,
          amount REAL,
          turnover_ratio REAL,
          source TEXT,
          updated_at TEXT,
          PRIMARY KEY (sec_type, sec_code, trade_date)
        )
        """
    )
    conn.commit()


def upsert_rows(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    df = pd.DataFrame(rows)
    for col in KLINE_COLS:
        if col not in df.columns:
            df[col] = None
    df = df[KLINE_COLS]
    sql = f"""
    INSERT OR REPLACE INTO kline_daily ({",".join(KLINE_COLS)})
    VALUES ({",".join(["?"] * len(KLINE_COLS))})
    """
    conn.executemany(sql, list(df.itertuples(index=False, name=None)))
    conn.commit()
    return len(df)


def stock_codes_from_db(conn: sqlite3.Connection, fallback_day: str | None = None) -> list[str]:
    rows: list[tuple[Any, ...]] = []
    try:
        rows = conn.execute("SELECT sec_code FROM stock_info ORDER BY sec_code").fetchall()
    except Exception:
        rows = []
    if not rows and fallback_day:
        rows = conn.execute(
            """
            SELECT DISTINCT sec_code
            FROM kline_daily
            WHERE sec_type='stock' AND trade_date=?
            ORDER BY sec_code
            """,
            (fallback_day,),
        ).fetchall()
    codes = []
    for row in rows:
        code = "".join(ch for ch in str(row[0]) if ch.isdigit())[-6:]
        if len(code) == 6:
            codes.append(code)
    return sorted(set(codes))


def existing_stock_completion_for_day(conn: sqlite3.Connection, trade_date: str) -> dict[str, dict[str, Any]]:
    try:
        rows = conn.execute(
            """
            SELECT sec_code, open, high, low, close, pre_close, volume, amount
            FROM kline_daily
            WHERE sec_type='stock' AND substr(replace(trade_date, '/', '-'), 1, 10)=?
            """,
            (trade_date,),
        ).fetchall()
    except Exception:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        code = "".join(ch for ch in str(row[0]) if ch.isdigit())[-6:]
        if len(code) == 6:
            values = dict(zip(("open", "high", "low", "close", "pre_close", "volume", "amount"), row[1:]))
            out[code] = classify_stock_row(values)
    return out


def existing_stock_codes_for_day(conn: sqlite3.Connection, trade_date: str) -> set[str]:
    """Return only existing rows that satisfy the shared completion contract."""
    return {
        code
        for code, classification in existing_stock_completion_for_day(conn, trade_date).items()
        if bool(classification["complete"])
    }


def _num(row: pd.Series, name: str) -> float | None:
    try:
        value = pd.to_numeric(row.get(name), errors="coerce")
        if pd.isna(value):
            return None
        return float(value)
    except Exception:
        return None


def fetch_stock_em(code: str, day_dash: str, day_compact: str) -> dict[str, Any]:
    import akshare as ak  # type: ignore

    df = ak.stock_zh_a_hist(
        symbol=code,
        period="daily",
        start_date=day_compact,
        end_date=day_compact,
        adjust="",
        timeout=10,
    )
    if df is None or df.empty:
        raise RuntimeError("empty dataframe")
    row = df.iloc[-1]
    volume_hands = _num(row, "成交量")
    close = _num(row, "收盘")
    pre_close = None
    change = _num(row, "涨跌额")
    if close is not None and change is not None:
        pre_close = close - change
    return {
        "sec_type": "stock",
        "sec_code": code,
        "trade_date": day_dash,
        "open": _num(row, "开盘"),
        "high": _num(row, "最高"),
        "low": _num(row, "最低"),
        "close": close,
        "pre_close": pre_close,
        "change": change,
        "change_pct": _num(row, "涨跌幅"),
        "volume": volume_hands * 100.0 if volume_hands is not None else None,
        "amount": _num(row, "成交额"),
        "turnover_ratio": _num(row, "换手率"),
        "source": "akshare_stock_zh_a_hist",
        "updated_at": now_iso(),
    }


def fetch_stock_sina(code: str, day_dash: str, day_compact: str) -> dict[str, Any]:
    import akshare as ak  # type: ignore

    symbol = market_prefix(code) + code
    df = ak.stock_zh_a_daily(symbol=symbol, start_date=day_compact, end_date=day_compact, adjust="")
    if df is None or df.empty:
        raise RuntimeError("empty dataframe")
    row = df.iloc[-1]
    return {
        "sec_type": "stock",
        "sec_code": code,
        "trade_date": day_dash,
        "open": _num(row, "open"),
        "high": _num(row, "high"),
        "low": _num(row, "low"),
        "close": _num(row, "close"),
        "pre_close": None,
        "change": None,
        "change_pct": None,
        "volume": _num(row, "volume"),
        "amount": _num(row, "amount"),
        "turnover_ratio": _num(row, "turnover"),
        "source": "akshare_stock_zh_a_daily",
        "updated_at": now_iso(),
    }


def fetch_stock_tx(code: str, day_dash: str, day_compact: str) -> dict[str, Any]:
    import akshare as ak  # type: ignore

    symbol = market_prefix(code) + code
    df = ak.stock_zh_a_hist_tx(
        symbol=symbol,
        start_date=day_compact,
        end_date=day_compact,
        adjust="",
        timeout=10,
    )
    if df is None or df.empty:
        raise RuntimeError("empty dataframe")
    row = df.iloc[-1]
    volume_hands = _num(row, "amount")
    return {
        "sec_type": "stock",
        "sec_code": code,
        "trade_date": day_dash,
        "open": _num(row, "open"),
        "high": _num(row, "high"),
        "low": _num(row, "low"),
        "close": _num(row, "close"),
        "pre_close": None,
        "change": None,
        "change_pct": None,
        "volume": volume_hands * 100.0 if volume_hands is not None else None,
        "amount": None,
        "turnover_ratio": None,
        "source": "akshare_stock_zh_a_hist_tx",
        "updated_at": now_iso(),
    }


_BAOSTOCK_LEGACY: Any | None = None


def fetch_stock_baostock(code: str, day_dash: str, _day_compact: str) -> dict[str, Any]:
    """Map the existing BaoStock backfill row contract into one repair row.

    ``_provider_pass_worker`` owns one BaoStock login/logout for the entire
    pass.  The mapping intentionally reuses ``fetch_kline_baostock`` from the
    existing backfill script so its OHLC, preclose, volume and amount semantics
    remain identical to the established source.
    """
    legacy = _BAOSTOCK_LEGACY
    if legacy is None:
        raise RuntimeError("baostock session is not active for this provider pass")
    bs_code = f"{market_prefix(code)}.{code}"
    frame = legacy.fetch_kline_baostock(bs_code, day_dash, day_dash)
    if frame is None or frame.empty:
        raise RuntimeError("empty dataframe")
    row = frame.iloc[-1]
    return {
        "sec_type": "stock",
        "sec_code": code,
        "trade_date": day_dash,
        "open": _num(row, "open"),
        "high": _num(row, "high"),
        "low": _num(row, "low"),
        "close": _num(row, "close"),
        "pre_close": _num(row, "pre_close"),
        "change": _num(row, "change"),
        "change_pct": _num(row, "change_pct"),
        "volume": _num(row, "volume"),
        "amount": _num(row, "amount"),
        "turnover_ratio": _num(row, "turnover_ratio"),
        "source": "baostock",
        "updated_at": now_iso(),
    }


def fetch_index_sina(code: str, day_dash: str, day_compact: str) -> dict[str, Any]:
    import akshare as ak  # type: ignore

    symbol = index_market_prefix(code) + code
    df = ak.stock_zh_a_daily(symbol=symbol, start_date=day_compact, end_date=day_compact, adjust="")
    if df is None or df.empty:
        raise RuntimeError("empty dataframe")
    row = df.iloc[-1]
    return {
        "sec_type": "index",
        "sec_code": code,
        "trade_date": day_dash,
        "open": _num(row, "open"),
        "high": _num(row, "high"),
        "low": _num(row, "low"),
        "close": _num(row, "close"),
        "pre_close": None,
        "change": None,
        "change_pct": None,
        "volume": _num(row, "volume"),
        "amount": _num(row, "amount"),
        "turnover_ratio": _num(row, "turnover"),
        "source": "akshare_stock_zh_a_daily_index",
        "updated_at": now_iso(),
    }


def fetch_index_tx(code: str, day_dash: str, day_compact: str) -> dict[str, Any]:
    import akshare as ak  # type: ignore

    symbol = index_market_prefix(code) + code
    df = ak.stock_zh_a_hist_tx(
        symbol=symbol,
        start_date=day_compact,
        end_date=day_compact,
        adjust="",
        timeout=10,
    )
    if df is None or df.empty:
        raise RuntimeError("empty dataframe")
    row = df.iloc[-1]
    volume_hands = _num(row, "amount")
    return {
        "sec_type": "index",
        "sec_code": code,
        "trade_date": day_dash,
        "open": _num(row, "open"),
        "high": _num(row, "high"),
        "low": _num(row, "low"),
        "close": _num(row, "close"),
        "pre_close": None,
        "change": None,
        "change_pct": None,
        "volume": volume_hands * 100.0 if volume_hands is not None else None,
        "amount": None,
        "turnover_ratio": None,
        "source": "akshare_stock_zh_a_hist_tx_index",
        "updated_at": now_iso(),
    }



def _latest_stock_close_before(conn: sqlite3.Connection, code: str, trade_date: str) -> float | None:
    row = conn.execute(
        """
        SELECT close
        FROM kline_daily
        WHERE sec_type='stock'
          AND sec_code=?
          AND trade_date < ?
          AND close IS NOT NULL
        ORDER BY trade_date DESC
        LIMIT 1
        """,
        (code, trade_date),
    ).fetchone()
    if row is None or row[0] is None:
        return None
    try:
        return float(row[0])
    except Exception:
        return None


def _passes_continuity_guard(row: dict[str, Any], previous_close: float | None, max_jump: float = MAX_FALLBACK_CLOSE_JUMP) -> bool:
    if row.get("sec_type") != "stock":
        return True
    classification = classify_stock_row(row)
    if classification["complete"] and classification["row_status"] != ROW_VALID_TRADE:
        return True
    close = row.get("close")
    try:
        close_value = float(close)
    except Exception:
        return True
    if previous_close is None or previous_close <= 0:
        return True
    return abs(close_value / previous_close - 1.0) <= float(max_jump)


def _filter_continuous_stock_rows(
    conn: sqlite3.Connection,
    rows: list[dict[str, Any]],
    *,
    max_jump: float = MAX_FALLBACK_CLOSE_JUMP,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for row in rows:
        previous_close = _latest_stock_close_before(conn, str(row.get("sec_code", "")), str(row.get("trade_date", "")))
        if _passes_continuity_guard(row, previous_close, max_jump=max_jump):
            accepted.append(row)
        else:
            blocked = dict(row)
            blocked["previous_close"] = previous_close
            rejected.append(blocked)
    return accepted, rejected


def _provider_summary(provider: str, before_missing: int) -> dict[str, Any]:
    return {
        "provider": provider,
        "attempted": 0,
        "valid": 0,
        "empty": 0,
        "error": 0,
        "invalid": 0,
        "timeout": 0,
        "saturated": 0,
        "circuit_reason": None,
        "circuit_error_mode": "consecutive_provider_errors",
        "max_consecutive_errors": 0,
        "checkpointed": 0,
        "guard_rejected": 0,
        "elapsed": 0.0,
        "rows_per_minute": 0.0,
        "before_missing": int(before_missing),
        "after_missing": int(before_missing),
        "error_samples": [],
    }


def _add_error_sample(summary: dict[str, Any], message: str) -> None:
    samples = summary["error_samples"]
    if len(samples) < 8:
        samples.append(str(message)[:220])


def _stock_row_is_complete(row: dict[str, Any]) -> tuple[bool, str]:
    classification = classify_stock_row(row)
    if bool(classification["complete"]):
        return True, ""
    return False, f"{classification['row_status']}:{classification['reason']}"


def _provider_pass_worker(
    queue: Any,
    source_name: str,
    fn: Callable[[str, str, str], dict[str, Any]],
    codes: list[str],
    day_dash: str,
    day_compact: str,
    min_request_interval_sec: float,
) -> None:
    """Run one provider pass in a killable child process.

    A timeout is observed by the parent and terminates this whole process.  No
    provider call is left in a daemon thread, and no retained semaphore can
    cascade into a false `in-flight limit reached` storm.
    """
    global _BAOSTOCK_LEGACY
    legacy = None
    try:
        if source_name == "baostock":
            legacy = importlib.import_module("backfill_baostock_hsA_60d_v2")
            legacy.bs_login()
            _BAOSTOCK_LEGACY = legacy
        pacer = GlobalRequestPacer(min_request_interval_sec)
        for code in codes:
            queue.put(("started", code, None))
            try:
                pacer.wait()
                row = fn(code, day_dash, day_compact)
                complete, detail = _stock_row_is_complete(row)
                if complete:
                    queue.put(("row", code, row))
                else:
                    queue.put(("invalid", code, detail))
            except BaseException as exc:
                queue.put(("error", code, f"{type(exc).__name__}: {exc}"))
        queue.put(("done", None, None))
    except BaseException as exc:
        queue.put(("fatal", None, f"{type(exc).__name__}: {exc}"))
    finally:
        if legacy is not None:
            try:
                legacy.bs_logout()
            finally:
                _BAOSTOCK_LEGACY = None


def _terminate_provider_process(process: Any) -> None:
    if process.is_alive():
        process.terminate()
        process.join(1.0)
    if process.is_alive():
        process.kill()
        process.join(1.0)


def _should_isolate_provider(
    fn: Callable[[str, str, str], dict[str, Any]],
    isolate: bool | None,
) -> bool:
    if isolate is not None:
        return bool(isolate)
    # Production fetchers are top-level functions in this module.  Unit tests
    # commonly inject local mocks, which cannot be pickled by Windows spawn and
    # are intentionally executed synchronously inside the test process.
    return getattr(fn, "__module__", "") == __name__ and "<locals>" not in getattr(fn, "__qualname__", "")


def _run_stock_provider_pass(
    codes: list[str],
    day_dash: str,
    day_compact: str,
    *,
    provider: str,
    fn: Callable[[str, str, str], dict[str, Any]],
    min_request_interval_sec: float,
    provider_timeout_sec: float,
    error_threshold: int,
    isolate: bool | None,
    on_checkpoint: Callable[[list[dict[str, Any]], str], Iterable[str]] | None = None,
    checkpoint_size: int = 200,
) -> tuple[list[dict[str, Any]], set[str], dict[str, Any]]:
    """Fetch one provider stage and return checkpointed rows plus its remaining gap.

    A row is removed from ``pending`` only after the parent process accepts it
    at a checkpoint.  Provider children never receive a database handle.  Row
    quality failures and empty responses remain unresolved for the next source;
    only consecutive non-empty provider exceptions contribute to the source
    circuit, and any complete row resets that counter.
    """
    pending = {str(code) for code in codes}
    accepted: list[dict[str, Any]] = []
    checkpoint_buffer: list[dict[str, Any]] = []
    summary = _provider_summary(provider, len(pending))
    started = time.monotonic()
    threshold = max(1, int(error_threshold))
    batch_size = max(1, int(checkpoint_size))
    consecutive_provider_errors = 0

    def checkpoint_rows() -> None:
        nonlocal checkpoint_buffer
        if not checkpoint_buffer:
            return
        rows = checkpoint_buffer
        checkpoint_buffer = []
        candidate_codes = {str(row.get("sec_code") or "") for row in rows}
        if on_checkpoint is None:
            persisted_codes = candidate_codes
        else:
            persisted_codes = {str(code) for code in on_checkpoint(rows, provider)}
            persisted_codes &= candidate_codes
        accepted.extend(row for row in rows if str(row.get("sec_code") or "") in persisted_codes)
        pending.difference_update(persisted_codes)
        summary["checkpointed"] += len(persisted_codes)
        summary["guard_rejected"] += len(candidate_codes - persisted_codes)

    def accept_complete_row(code: str, row: dict[str, Any]) -> None:
        nonlocal consecutive_provider_errors
        checkpoint_buffer.append(row)
        summary["valid"] += 1
        consecutive_provider_errors = 0
        if len(checkpoint_buffer) >= batch_size:
            checkpoint_rows()

    def consume(kind: str, code: str | None, payload: Any) -> bool:
        nonlocal consecutive_provider_errors
        if kind == "started":
            summary["attempted"] += 1
            return False
        if kind == "row" and code is not None:
            accept_complete_row(code, payload)
            return False
        if kind == "invalid" and code is not None:
            # A response that fails the row-quality contract is not a source
            # transport/provider error.  It stays in the gap for a fallback
            # but breaks an exception streak so three scattered bad symbols do
            # not trip a healthy provider's circuit.
            consecutive_provider_errors = 0
            summary["invalid"] += 1
            _add_error_sample(summary, f"{code} invalid {payload}")
        elif kind in {"error", "fatal"}:
            message = str(payload)
            if "empty" in message.lower():
                # An empty but well-formed provider response also remains
                # unresolved without being treated as a transport failure.
                consecutive_provider_errors = 0
                summary["empty"] += 1
            else:
                summary["error"] += 1
                consecutive_provider_errors += 1
                summary["max_consecutive_errors"] = max(
                    int(summary["max_consecutive_errors"]),
                    consecutive_provider_errors,
                )
            _add_error_sample(summary, f"{code or provider} {message}")
            if kind == "fatal":
                summary["circuit_reason"] = "worker_error"
                return True
        if consecutive_provider_errors >= threshold:
            summary["circuit_reason"] = "error_threshold"
            return True
        return False

    if not pending:
        return accepted, pending, summary

    try:
        if not _should_isolate_provider(fn, isolate):
            # Test-injected non-picklable callables never use a thread timeout.
            # Production fetchers always use the killable process boundary below.
            pacer = GlobalRequestPacer(min_request_interval_sec)
            for code in list(codes):
                summary["attempted"] += 1
                try:
                    pacer.wait()
                    row = fn(code, day_dash, day_compact)
                    complete, detail = _stock_row_is_complete(row)
                    if complete:
                        accept_complete_row(code, row)
                    elif consume("invalid", code, detail):
                        break
                except Exception as exc:
                    if consume("error", code, f"{type(exc).__name__}: {exc}"):
                        break
        else:
            context = mp.get_context("spawn")
            queue = context.Queue()
            process = context.Process(
                target=_provider_pass_worker,
                args=(queue, provider, fn, list(codes), day_dash, day_compact, min_request_interval_sec),
            )
            circuit_open = False
            last_activity = time.monotonic()
            try:
                process.start()
                while True:
                    try:
                        kind, code, payload = queue.get(timeout=0.05)
                        last_activity = time.monotonic()
                        if kind == "done":
                            break
                        if consume(kind, code, payload):
                            circuit_open = True
                            break
                    except Empty:
                        if not process.is_alive():
                            break
                        if time.monotonic() - last_activity >= max(0.1, float(provider_timeout_sec)):
                            summary["timeout"] += 1
                            summary["circuit_reason"] = "timeout"
                            _add_error_sample(summary, f"{provider} exceeded {provider_timeout_sec:g}s")
                            circuit_open = True
                            break
                if circuit_open:
                    _terminate_provider_process(process)
                else:
                    process.join(1.0)
                    if process.is_alive():
                        summary["timeout"] += 1
                        summary["circuit_reason"] = "timeout"
                        _terminate_provider_process(process)
                    elif process.exitcode not in (0, None) and summary["circuit_reason"] is None:
                        summary["circuit_reason"] = "worker_exit"
                        summary["error"] += 1
                        _add_error_sample(summary, f"{provider} worker exit={process.exitcode}")
            except Exception as exc:
                summary["circuit_reason"] = "process_start_error"
                summary["error"] += 1
                _add_error_sample(summary, f"{type(exc).__name__}: {exc}")
                _terminate_provider_process(process)
            finally:
                queue.close()
                queue.join_thread()
    finally:
        # A completed pass, a circuit, or a Python exception all flush what the
        # parent has already received.  A hard outer kill may skip this final
        # flush, but bounded earlier checkpoints remain durable.
        checkpoint_rows()
        summary["elapsed"] = round(time.monotonic() - started, 3)
        summary["rows_per_minute"] = round(
            (60.0 * float(summary["valid"]) / summary["elapsed"]) if summary["elapsed"] else 0.0,
            3,
        )
    return accepted, pending, summary


def run_stock_provider_passes(
    codes: list[str],
    day_dash: str,
    day_compact: str,
    *,
    sources: list[tuple[str, Callable[[str, str, str], dict[str, Any]]]],
    min_request_interval_sec: float,
    provider_timeout_sec: float = PROVIDER_CALL_TIMEOUT_SEC,
    error_threshold: int = PROVIDER_ERROR_CIRCUIT_THRESHOLD,
    isolate: bool | None = None,
    on_checkpoint: Callable[[list[dict[str, Any]], str], Iterable[str]] | None = None,
    checkpoint_size: int = 200,
    on_summary: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """Run stock sources as sequential provider passes over the dynamic gap.

    BaoStock is primary, Sina only sees codes that BaoStock did not complete,
    and Eastmoney requires a same-run one-code probe before its full pass.  The
    Tencent stock source is deliberately absent because it cannot supply
    `amount`; index fallbacks retain their existing behaviour elsewhere.
    """
    pending = list(dict.fromkeys(str(code) for code in codes))
    rows: list[dict[str, Any]] = []
    summaries: list[dict[str, Any]] = []

    def record_summary(summary: dict[str, Any]) -> None:
        summaries.append(summary)
        if on_summary is not None:
            on_summary(summary)

    for provider, fn in sources:
        if not pending:
            break
        if provider == "ak_em":
            probe_rows, probe_remaining, probe_summary = _run_stock_provider_pass(
                pending[:1],
                day_dash,
                day_compact,
                provider=provider,
                fn=fn,
                min_request_interval_sec=min_request_interval_sec,
                provider_timeout_sec=provider_timeout_sec,
                error_threshold=error_threshold,
                isolate=isolate,
                on_checkpoint=on_checkpoint,
                checkpoint_size=checkpoint_size,
            )
            probe_summary["probe"] = True
            rows.extend(probe_rows)
            # ``probe_remaining`` describes only ``probe_codes``.  Remove
            # solely the codes that the probe actually checkpointed; filtering
            # all of ``pending`` by that one-code set used to erase unprobed
            # Eastmoney work and falsely report the gap as resolved.
            probe_codes = pending[:1]
            resolved_probe_codes = set(probe_codes) - probe_remaining
            pending = [code for code in pending if code not in resolved_probe_codes]
            if not resolved_probe_codes:
                if probe_summary["circuit_reason"] is None:
                    probe_summary["circuit_reason"] = "probe_failed"
                probe_summary["after_missing"] = len(pending)
                record_summary(probe_summary)
                continue
            probe_summary["after_missing"] = len(pending)
            record_summary(probe_summary)
            if not pending:
                break
        pass_rows, remaining, summary = _run_stock_provider_pass(
            pending,
            day_dash,
            day_compact,
            provider=provider,
            fn=fn,
            min_request_interval_sec=min_request_interval_sec,
            provider_timeout_sec=provider_timeout_sec,
            error_threshold=error_threshold,
            isolate=isolate,
            on_checkpoint=on_checkpoint,
            checkpoint_size=checkpoint_size,
        )
        rows.extend(pass_rows)
        pending = [code for code in pending if code in remaining]
        summary["after_missing"] = len(pending)
        record_summary(summary)
    return rows, summaries, pending


def fetch_with_sources(
    code: str,
    day_dash: str,
    day_compact: str,
    sources: list[tuple[str, Callable[[str, str, str], dict[str, Any]]]],
    *,
    attempts_per_source: int,
    pacer: GlobalRequestPacer | None = None,
    require_complete_stock: bool = False,
) -> tuple[dict[str, Any] | None, list[str]]:
    errors: list[str] = []
    for source_name, fn in sources:
        quality_rejected = False
        for attempt in range(1, attempts_per_source + 1):
            try:
                if pacer is not None:
                    pacer.wait()
                row = fn(code, day_dash, day_compact)
                if require_complete_stock or row.get("sec_type") == "stock":
                    classification = classify_stock_row(row)
                    if not classification["complete"]:
                        errors.append(
                            f"{source_name} rejected source={row.get('source') or source_name} "
                            f"status={classification['row_status']} reason={classification['reason']}"
                        )
                        quality_rejected = True
                        break
                return row, errors
            except Exception as exc:
                errors.append(f"{source_name} attempt {attempt}: {type(exc).__name__}: {str(exc)[:180]}")
        if not quality_rejected:
            errors.append(f"{source_name}: stopped after {attempts_per_source} failed attempts")
    return None, errors


def repair_day(
    db_path: str | Path,
    trade_date: str,
    *,
    limit: int = 0,
    workers: int = DEFAULT_STOCK_WORKERS,
    attempts_per_source: int = 3,
    min_request_interval_sec: float = DEFAULT_STOCK_REQUEST_INTERVAL_SEC,
    provider_timeout_sec: float = PROVIDER_CALL_TIMEOUT_SEC,
    checkpoint_size: int = 200,
) -> dict[str, Any]:
    day_dash, day_compact = normalize_day(trade_date)
    conn = connect(db_path)
    try:
        ensure_schema(conn)
        fallback_day = conn.execute(
            "SELECT MAX(trade_date) FROM kline_daily WHERE sec_type='stock'"
        ).fetchone()[0]
        codes = stock_codes_from_db(conn, fallback_day=fallback_day)
        existing_completion = existing_stock_completion_for_day(conn, day_dash)
    finally:
        conn.close()

    total_codes = len(codes)
    existing_codes = {code for code, item in existing_completion.items() if bool(item["complete"])}
    retryable_existing = {code for code, item in existing_completion.items() if bool(item["retryable"])}
    if existing_codes:
        codes = [code for code in codes if code not in existing_codes]

    if limit > 0:
        codes = codes[:limit]

    stock_sources = [
        ("baostock", fetch_stock_baostock),
        ("ak_sina", fetch_stock_sina),
        ("ak_em", fetch_stock_em),
    ]
    index_sources = [
        ("ak_sina", fetch_index_sina),
        ("ak_tx", fetch_index_tx),
    ]
    pacer = GlobalRequestPacer(min_request_interval_sec)
    started = time.time()
    index_rows: list[dict[str, Any]] = []
    index_failures: dict[str, list[str]] = {}
    for code in REQUIRED_INDEX_CODES:
        row, errs = fetch_with_sources(
            code,
            day_dash,
            day_compact,
            index_sources,
            attempts_per_source=attempts_per_source,
            pacer=pacer,
        )
        if row is None:
            index_failures[code] = errs[-8:]
        else:
            index_rows.append(row)

    conn = connect(db_path)
    try:
        saved_index = upsert_rows(conn, index_rows)
        print(
            f"[INDEX] saved={saved_index}/{len(REQUIRED_INDEX_CODES)} "
            f"failures={len(index_failures)} elapsed={time.time() - started:.1f}s",
            flush=True,
        )
    finally:
        conn.close()

    guard_rejections: list[dict[str, Any]] = []
    saved_stock = 0

    def checkpoint_stock_rows(rows: list[dict[str, Any]], _provider: str) -> set[str]:
        """Persist only parent-approved rows; rejected codes stay unresolved."""
        nonlocal saved_stock
        conn = connect(db_path)
        try:
            accepted, rejected = _filter_continuous_stock_rows(conn, rows)
            guard_rejections.extend(rejected)
            saved_stock += upsert_rows(conn, accepted)
            return {str(row.get("sec_code") or "") for row in accepted}
        finally:
            conn.close()

    def emit_provider_summary(summary: dict[str, Any]) -> None:
        # Emitted at every pass end (including a circuit), so an outer deadline
        # never hides which source has already checkpointed progress.
        print("PROVIDER_SUMMARY=" + json.dumps(summary, ensure_ascii=False, sort_keys=True), flush=True)

    rows, provider_summary, unresolved_codes = run_stock_provider_passes(
        codes,
        day_dash,
        day_compact,
        sources=stock_sources,
        min_request_interval_sec=min_request_interval_sec,
        provider_timeout_sec=provider_timeout_sec,
        on_checkpoint=checkpoint_stock_rows,
        checkpoint_size=checkpoint_size,
        on_summary=emit_provider_summary,
    )

    provider_quality_rejections = {
        str(summary["provider"]): list(summary.get("error_samples") or [])
        for summary in provider_summary
        if summary.get("error_samples")
    }
    print(
        f"[STOCK] passes={len(provider_summary)} saved={saved_stock} "
        f"remaining={len(unresolved_codes)} guard_rejected={len(guard_rejections)} "
        f"elapsed={time.time() - started:.1f}s",
        flush=True,
    )

    conn = connect(db_path)
    try:
        stock_count = conn.execute(
            "SELECT COUNT(*) FROM kline_daily WHERE sec_type='stock' AND trade_date=?",
            (day_dash,),
        ).fetchone()[0]
        index_count = conn.execute(
            "SELECT COUNT(*) FROM kline_daily WHERE sec_type='index' AND trade_date=?",
            (day_dash,),
        ).fetchone()[0]
        postcondition = raw_market_postcondition(conn, day_dash)
    finally:
        conn.close()

    return {
        "trade_date": day_dash,
        "stock_count": int(stock_count),
        "index_count": int(index_count),
        "saved_index": int(saved_index),
        "total_codes": int(total_codes),
        "pending_codes": int(len(codes)),
        "skipped_existing": int(len(existing_codes)),
        "retryable_existing": int(len(retryable_existing)),
        "stock_failures": {"unresolved_count": int(len(unresolved_codes)), "sample_codes": unresolved_codes[:50]},
        "provider_quality_rejections": provider_quality_rejections,
        "provider_summary": provider_summary,
        "index_failures": index_failures,
        "guard_rejections": guard_rejections[:50],
        "guard_rejection_count": int(len(guard_rejections)),
        "workers": int(DEFAULT_STOCK_WORKERS),
        "min_request_interval_sec": float(max(0.0, min_request_interval_sec)),
        "provider_timeout_sec": float(max(0.1, provider_timeout_sec)),
        "checkpoint_size": int(max(1, checkpoint_size)),
        "provider_timed_out_calls": sum(int(summary.get("timeout", 0)) for summary in provider_summary),
        "provider_saturated_calls": sum(int(summary.get("saturated", 0)) for summary in provider_summary),
        "raw_postcondition": postcondition,
        "elapsed_sec": round(time.time() - started, 1),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Repair one market day using AkShare fallback sources.")
    parser.add_argument("--db", default="a_share_mvp.db")
    parser.add_argument("--date", required=True)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--workers", type=int, default=DEFAULT_STOCK_WORKERS)
    parser.add_argument("--attempts-per-source", type=int, default=3)
    parser.add_argument("--min-request-interval-sec", type=float, default=DEFAULT_STOCK_REQUEST_INTERVAL_SEC)
    parser.add_argument("--provider-timeout-sec", type=float, default=PROVIDER_CALL_TIMEOUT_SEC)
    args = parser.parse_args()

    result = repair_day(
        args.db,
        args.date,
        limit=max(0, int(args.limit)),
        workers=max(1, int(args.workers)),
        attempts_per_source=max(1, min(3, int(args.attempts_per_source))),
        min_request_interval_sec=max(0.0, float(args.min_request_interval_sec)),
        provider_timeout_sec=max(0.1, float(args.provider_timeout_sec)),
    )
    compact = {
        key: value
        for key, value in result.items()
        if key not in {"stock_failures", "provider_quality_rejections", "index_failures", "guard_rejections"}
    }
    print("REPAIR_RESULT=" + json.dumps(compact, ensure_ascii=False, sort_keys=True))
    return 0 if bool(result["raw_postcondition"]["ok"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
