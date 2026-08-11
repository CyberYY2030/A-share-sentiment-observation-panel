from __future__ import annotations

import argparse
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

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


class ProviderCallLimiter:
    """Bound one provider call without allowing a stalled fallback to block repair workers."""

    def __init__(self, *, max_inflight: int, timeout_sec: float) -> None:
        self._max_inflight = max(1, int(max_inflight))
        self._timeout_sec = max(0.1, float(timeout_sec))
        self._lock = threading.Lock()
        self._slots: dict[str, threading.BoundedSemaphore] = {}
        self._timed_out_calls = 0
        self._saturated_calls = 0

    @property
    def timed_out_calls(self) -> int:
        with self._lock:
            return self._timed_out_calls

    @property
    def saturated_calls(self) -> int:
        with self._lock:
            return self._saturated_calls

    def call(
        self,
        source_name: str,
        fn: Callable[[str, str, str], dict[str, Any]],
        code: str,
        day_dash: str,
        day_compact: str,
        *,
        pacer: GlobalRequestPacer | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            slot = self._slots.setdefault(source_name, threading.BoundedSemaphore(self._max_inflight))
        if not slot.acquire(blocking=False):
            with self._lock:
                self._saturated_calls += 1
            raise TimeoutError(f"{source_name} in-flight limit reached")

        state: dict[str, Any] = {}

        def invoke() -> None:
            try:
                if pacer is not None:
                    pacer.wait()
                state["row"] = fn(code, day_dash, day_compact)
            except BaseException as exc:
                state["error"] = exc
            finally:
                slot.release()

        worker = threading.Thread(target=invoke, daemon=True)
        worker.start()
        worker.join(self._timeout_sec)
        if worker.is_alive():
            with self._lock:
                self._timed_out_calls += 1
            raise TimeoutError(f"{source_name} exceeded {self._timeout_sec:g}s")
        if "error" in state:
            raise state["error"]
        return state["row"]


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


def fetch_with_sources(
    code: str,
    day_dash: str,
    day_compact: str,
    sources: list[tuple[str, Callable[[str, str, str], dict[str, Any]]]],
    *,
    attempts_per_source: int,
    pacer: GlobalRequestPacer | None = None,
    provider_calls: ProviderCallLimiter | None = None,
    require_complete_stock: bool = False,
) -> tuple[dict[str, Any] | None, list[str]]:
    errors: list[str] = []
    for source_name, fn in sources:
        quality_rejected = False
        for attempt in range(1, attempts_per_source + 1):
            try:
                if provider_calls is not None:
                    row = provider_calls.call(
                        source_name,
                        fn,
                        code,
                        day_dash,
                        day_compact,
                        pacer=pacer,
                    )
                else:
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
    workers: int = 4,
    attempts_per_source: int = 3,
    min_request_interval_sec: float = 0.0,
    provider_timeout_sec: float = PROVIDER_CALL_TIMEOUT_SEC,
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
        ("ak_em", fetch_stock_em),
        ("ak_sina", fetch_stock_sina),
        ("ak_tx", fetch_stock_tx),
    ]
    index_sources = [
        ("ak_sina", fetch_index_sina),
        ("ak_tx", fetch_index_tx),
    ]
    pacer = GlobalRequestPacer(min_request_interval_sec)
    provider_calls = ProviderCallLimiter(
        max_inflight=max(1, workers),
        timeout_sec=provider_timeout_sec,
    )

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
            provider_calls=provider_calls,
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

    rows: list[dict[str, Any]] = []
    failures: dict[str, list[str]] = {}
    provider_quality_rejections: dict[str, list[str]] = {}
    guard_rejections: list[dict[str, Any]] = []

    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {
            executor.submit(
                fetch_with_sources,
                code,
                day_dash,
                day_compact,
                stock_sources,
                attempts_per_source=attempts_per_source,
                pacer=pacer,
                provider_calls=provider_calls,
                require_complete_stock=True,
            ): code
            for code in codes
        }
        for i, future in enumerate(as_completed(futures), start=1):
            code = futures[future]
            row, errs = future.result()
            quality_errors = [error for error in errs if " rejected source=" in error]
            if quality_errors:
                provider_quality_rejections[code] = quality_errors[-8:]
            if row is None:
                failures[code] = errs[-8:]
            else:
                rows.append(row)
            if i % 200 == 0 or i == len(futures):
                conn = connect(db_path)
                try:
                    accepted, rejected = _filter_continuous_stock_rows(conn, rows)
                    guard_rejections.extend(rejected)
                    n = upsert_rows(conn, accepted)
                    rows = []
                    print(
                        f"[STOCK] progress {i}/{len(futures)} saved_batch={n} "
                        f"guard_rejected={len(guard_rejections)} "
                        f"failures={len(failures)} elapsed={time.time() - started:.1f}s",
                        flush=True,
                    )
                finally:
                    conn.close()

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
        "stock_failures": failures,
        "provider_quality_rejections": provider_quality_rejections,
        "index_failures": index_failures,
        "guard_rejections": guard_rejections[:50],
        "guard_rejection_count": int(len(guard_rejections)),
        "workers": int(max(1, workers)),
        "min_request_interval_sec": float(max(0.0, min_request_interval_sec)),
        "provider_timeout_sec": float(max(0.1, provider_timeout_sec)),
        "provider_timed_out_calls": provider_calls.timed_out_calls,
        "provider_saturated_calls": provider_calls.saturated_calls,
        "raw_postcondition": postcondition,
        "elapsed_sec": round(time.time() - started, 1),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Repair one market day using AkShare fallback sources.")
    parser.add_argument("--db", default="a_share_mvp.db")
    parser.add_argument("--date", required=True)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--attempts-per-source", type=int, default=3)
    parser.add_argument("--min-request-interval-sec", type=float, default=0.0)
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
    print(result)
    return 0 if bool(result["raw_postcondition"]["ok"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
