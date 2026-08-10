from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable

from mining.data_quality import (
    STATUS_BAD,
    STATUS_CLEAN,
    STATUS_USABLE_WITH_QUARANTINE,
    inspect_stock_session,
    mark_known_bad_session,
)
from runtime_paths import build_runtime_paths


REQUIRED_INDEX_CODES = ("000001", "399001", "000300", "000852")
MAX_INTERFACE_ATTEMPTS = 3
KLINE_COLS = (
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
)
SCRIPT_BAOSTOCK_CANDIDATES = ("backfill_baostock_hsA_60d_v2.py", "backfill_baostock_hsA_60d.py")
SCRIPT_CONCEPT_CANDIDATES = (
    "backfill_adata_ths_concept_index_kline_60d_v3.py",
    "backfill_adata_ths_concept_index_kline_60d_v2.py",
    "backfill_adata_ths_concept_index_kline_60d.py",
)
SCRIPT_ETF_CANDIDATES = ("backfill_etf_equity_60d_v2.py", "backfill_etf_equity_60d.py")
SCRIPT_REPAIR_MARKET_DAY_CANDIDATES = ("repair_market_day_akshare.py",)


def normalize_day(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip().replace("/", "-")
    if not text:
        return None
    if len(text) >= 8 and text[:8].isdigit() and "-" not in text[:8]:
        text = f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    try:
        return dt.date.fromisoformat(text[:10]).isoformat()
    except Exception:
        return None


def _connect(db_path: str | Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    return con


def _table_exists(con: sqlite3.Connection, table: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table,),
    ).fetchone()
    return row is not None


def _scalar(con: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> Any:
    row = con.execute(sql, params).fetchone()
    return row[0] if row else None


def _canonical_code(value: Any) -> str:
    text = "" if value is None else str(value).strip()
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) >= 6:
        return digits[-6:]
    return digits.zfill(6) if digits else ""


def _existing_domains(base_dir: str | Path) -> list[str]:
    paths = build_runtime_paths(str(base_dir))
    domains = ["stock", "index"]
    if Path(paths.concept_db).exists():
        domains.append("concept")
    if Path(paths.etf_db).exists():
        domains.append("etf")
    if (Path(paths.base_dir) / "mining_mvp.db").exists():
        domains.append("mining")
    return domains


def stock_coverage_for_date(
    stock_db: str | Path,
    day: str,
    *,
    stock_min_rows: int = 2000,
    required_index_codes: Iterable[str] = REQUIRED_INDEX_CODES,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "stock": False,
        "index": False,
        "stock_rows": 0,
        "index_codes": [],
        "session_quality": {},
    }
    if not Path(stock_db).exists():
        return result

    con = _connect(stock_db)
    try:
        if not _table_exists(con, "kline_daily"):
            return result
        date_expr = "substr(replace(trade_date, '/', '-'), 1, 10)"
        stock_rows = int(
            _scalar(
                con,
                f"""
                SELECT COUNT(DISTINCT sec_code)
                FROM kline_daily
                WHERE sec_type='stock' AND {date_expr}=?
                """,
                (day,),
            )
            or 0
        )
        rows = con.execute(
            f"""
            SELECT DISTINCT sec_code
            FROM kline_daily
            WHERE sec_type='index' AND {date_expr}=?
            """,
            (day,),
        ).fetchall()
        index_codes = sorted({_canonical_code(row[0]) for row in rows if row and row[0] is not None})
        required = {_canonical_code(code) for code in required_index_codes}

        session_quality = inspect_stock_session(con, day)
        result.update(
            {
                "stock": stock_rows >= int(stock_min_rows)
                and session_quality["status"] in {STATUS_CLEAN, STATUS_USABLE_WITH_QUARANTINE},
                "index": required.issubset(set(index_codes)),
                "stock_rows": stock_rows,
                "index_codes": index_codes,
                "session_quality": session_quality,
            }
        )
        return result
    finally:
        con.close()


def concept_coverage_for_date(
    concept_db: str | Path,
    day: str,
    *,
    concept_min_rows: int = 100,
) -> dict[str, Any]:
    result: dict[str, Any] = {"concept": False, "concept_rows": 0}
    if not Path(concept_db).exists():
        return result

    con = _connect(concept_db)
    try:
        table = "concept_kline" if _table_exists(con, "concept_kline") else None
        if table is None and _table_exists(con, "concept_kline_ths"):
            table = "concept_kline_ths"
        if table is None:
            return result
        rows = int(
            _scalar(
                con,
                f"""
                SELECT COUNT(*)
                FROM {table}
                WHERE substr(replace(trade_date, '/', '-'), 1, 10)=?
                """,
                (day,),
            )
            or 0
        )
        result.update({"concept": rows >= int(concept_min_rows), "concept_rows": rows})
        return result
    finally:
        con.close()


def etf_coverage_for_date(
    etf_db: str | Path,
    day: str,
    *,
    etf_min_rows: int = 100,
) -> dict[str, Any]:
    result: dict[str, Any] = {"etf": False, "etf_scale_rows": 0, "etf_total_rows": 0}
    if not Path(etf_db).exists():
        return result

    con = _connect(etf_db)
    try:
        total_rows = 0
        scale_rows = 0
        if _table_exists(con, "etf_total"):
            total_rows = int(
                _scalar(
                    con,
                    "SELECT COUNT(*) FROM etf_total WHERE substr(replace(trade_date, '/', '-'), 1, 10)=?",
                    (day,),
                )
                or 0
            )
        if _table_exists(con, "etf_scale"):
            scale_rows = int(
                _scalar(
                    con,
                    "SELECT COUNT(*) FROM etf_scale WHERE substr(replace(trade_date, '/', '-'), 1, 10)=?",
                    (day,),
                )
                or 0
            )
        result.update(
            {
                "etf": total_rows > 0 and scale_rows >= int(etf_min_rows),
                "etf_scale_rows": scale_rows,
                "etf_total_rows": total_rows,
            }
        )
        return result
    finally:
        con.close()


def mining_coverage_for_date(base_dir: str | Path, day: str) -> dict[str, Any]:
    db_path = Path(base_dir) / "mining_mvp.db"
    result: dict[str, Any] = {"mining": False, "mining_candidates": 0, "mining_runs": 0}
    if not db_path.exists():
        return result

    con = _connect(db_path)
    try:
        candidates = 0
        runs = 0
        if _table_exists(con, "candidates"):
            candidates = int(
                _scalar(
                    con,
                    "SELECT COUNT(*) FROM candidates WHERE substr(replace(trade_date, '/', '-'), 1, 10)=?",
                    (day,),
                )
                or 0
            )
        if _table_exists(con, "strategy_runs"):
            runs = int(
                _scalar(
                    con,
                    "SELECT COUNT(*) FROM strategy_runs WHERE substr(replace(trade_date, '/', '-'), 1, 10)=?",
                    (day,),
                )
                or 0
            )
        result.update(
            {
                "mining": candidates > 0 or runs > 0,
                "mining_candidates": candidates,
                "mining_runs": runs,
            }
        )
        return result
    finally:
        con.close()


def coverage_for_date(
    base_dir: str | Path,
    day: str,
    *,
    domains: Iterable[str] | None = None,
    stock_min_rows: int = 2000,
    concept_min_rows: int = 100,
    etf_min_rows: int = 100,
) -> dict[str, Any]:
    normalized = normalize_day(day)
    if normalized is None:
        raise ValueError(f"Invalid day: {day!r}")

    paths = build_runtime_paths(str(base_dir))
    selected = list(domains) if domains is not None else _existing_domains(base_dir)
    result: dict[str, Any] = {"trade_date": normalized}

    if "stock" in selected or "index" in selected:
        result.update(
            stock_coverage_for_date(
                paths.stock_db,
                normalized,
                stock_min_rows=stock_min_rows,
            )
        )
    if "concept" in selected:
        result.update(
            concept_coverage_for_date(
                paths.concept_db,
                normalized,
                concept_min_rows=concept_min_rows,
            )
        )
    if "etf" in selected:
        result.update(etf_coverage_for_date(paths.etf_db, normalized, etf_min_rows=etf_min_rows))
    if "mining" in selected:
        result.update(mining_coverage_for_date(paths.base_dir, normalized))
    return result


def build_missing_update_plan(
    base_dir: str | Path,
    expected_dates: Iterable[Any],
    *,
    domains: Iterable[str] | None = None,
    stock_min_rows: int = 2000,
    concept_min_rows: int = 100,
    etf_min_rows: int = 100,
) -> dict[str, Any]:
    normalized_dates = [d for d in (normalize_day(x) for x in expected_dates) if d]
    selected_domains = list(domains) if domains is not None else _existing_domains(base_dir)
    coverage: dict[str, dict[str, Any]] = {}
    missing_by_day: dict[str, list[str]] = {}
    missing_by_domain: dict[str, list[str]] = {domain: [] for domain in selected_domains}

    for day in normalized_dates:
        row = coverage_for_date(
            base_dir,
            day,
            domains=selected_domains,
            stock_min_rows=stock_min_rows,
            concept_min_rows=concept_min_rows,
            etf_min_rows=etf_min_rows,
        )
        coverage[day] = row
        missing = [domain for domain in selected_domains if not bool(row.get(domain))]
        if missing:
            missing_by_day[day] = missing
            for domain in missing:
                missing_by_domain.setdefault(domain, []).append(day)

    return {
        "expected_dates": normalized_dates,
        "domains": selected_domains,
        "coverage": coverage,
        "missing_by_day": missing_by_day,
        "missing_by_domain": {k: v for k, v in missing_by_domain.items() if v},
        "ok": not bool(missing_by_day),
    }


def _weekday_trade_days(asof: str, days: int) -> list[str]:
    end = dt.date.fromisoformat(asof)
    out: list[str] = []
    cursor = end
    while len(out) < days:
        if cursor.weekday() < 5:
            out.append(cursor.isoformat())
        cursor -= dt.timedelta(days=1)
    return sorted(out)


def _baostock_trade_days(asof: str, days: int) -> list[str]:
    try:
        import baostock as bs  # type: ignore
    except Exception:
        return []
    start = (dt.date.fromisoformat(asof) - dt.timedelta(days=max(days * 3, 30))).isoformat()
    lg = bs.login()
    try:
        if getattr(lg, "error_code", "") != "0":
            return []
        rs = bs.query_trade_dates(start_date=start, end_date=asof)
        out: list[str] = []
        while rs.error_code == "0" and rs.next():
            row = rs.get_row_data()
            if len(row) >= 2 and str(row[1]) == "1":
                day = normalize_day(row[0])
                if day:
                    out.append(day)
        return sorted(out)[-days:]
    finally:
        try:
            bs.logout()
        except Exception:
            pass


def _akshare_trade_days(asof: str, days: int) -> list[str]:
    try:
        import akshare as ak  # type: ignore
        import pandas as pd
    except Exception:
        return []

    try:
        cal = ak.tool_trade_date_hist_sina()
    except Exception:
        return []
    if cal is None or getattr(cal, "empty", True):
        return []

    try:
        col = cal.columns[0]
        series = (
            pd.to_datetime(cal[col], errors="coerce")
            .dropna()
            .dt.normalize()
            .dt.strftime("%Y-%m-%d")
            .drop_duplicates()
            .sort_values()
        )
        out = [day for day in series.tolist() if day <= asof]
        return out[-days:]
    except Exception:
        return []


def resolve_expected_trade_days(
    stock_db: str | Path,
    *,
    asof: str | None,
    days: int,
    use_baostock: bool = False,
) -> list[str]:
    normalized_asof = normalize_day(asof) if asof else None
    if normalized_asof is None:
        normalized_asof = dt.date.today().isoformat()

    if use_baostock:
        trade_days = _baostock_trade_days(normalized_asof, days)
        if trade_days:
            return trade_days[-days:]

    trade_days = _akshare_trade_days(normalized_asof, days)
    if trade_days:
        return trade_days[-days:]

    # Fallback deliberately includes weekdays missing from the DB, which catches middle gaps.
    return _weekday_trade_days(normalized_asof, days)


def _find_script(base_dir: str | Path, candidates: Iterable[str]) -> Path | None:
    base = Path(base_dir)
    for name in candidates:
        candidate = Path(name)
        if candidate.is_absolute() and candidate.exists():
            return candidate
        for root in (base, Path.cwd(), base / "scripts", Path.cwd() / "scripts"):
            path = root / name
            if path.exists():
                return path
    return None


def _run(cmd: list[str], *, cwd: str | Path, timeout_sec: int) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
        out = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
        return int(proc.returncode), out.strip()
    except Exception as exc:
        return 999, f"run failed: {exc}"


def _run_with_retries(
    cmd: list[str],
    *,
    cwd: str | Path,
    timeout_sec: int,
    attempts: int = MAX_INTERFACE_ATTEMPTS,
    runner: Any = _run,
) -> tuple[int, str, int]:
    outputs: list[str] = []
    last_rc = 999
    max_attempts = max(1, int(attempts))
    for attempt in range(1, max_attempts + 1):
        rc, out = runner(cmd, cwd=cwd, timeout_sec=timeout_sec)
        last_rc = int(rc)
        outputs.append(f"attempt {attempt}/{max_attempts} rc={rc}" + (f"\n{out}" if out else ""))
        if rc == 0:
            return int(rc), "\n".join(outputs), attempt
    outputs.append(f"stopped after {max_attempts} failed attempts")
    return last_rc, "\n".join(outputs), max_attempts


def _domain_days(plan: dict[str, Any], *domains: str) -> list[str]:
    missing = plan.get("missing_by_domain") or {}
    days: set[str] = set()
    for domain in domains:
        days.update(missing.get(domain, []) or [])
    return sorted(days)


def _mark_unrecoverable_bad_stock_sessions(
    stock_db: str | Path,
    days: Iterable[str],
    commands: Iterable[dict[str, Any]],
) -> list[str]:
    """Persist only observed bad sessions after the bounded repair source was tried."""
    attempted = {
        str(arg)
        for command in commands
        if command.get("domain") == "stock_index"
        for arg in (command.get("cmd") or [])
    }
    marked: list[str] = []
    if not attempted or not Path(stock_db).exists():
        return marked
    con = _connect(stock_db)
    try:
        for day in sorted({str(day) for day in days}):
            if day not in attempted:
                continue
            quality = inspect_stock_session(con, day)
            if quality["status"] != STATUS_BAD:
                continue
            errors = [
                str(command.get("output") or "")[-1000:]
                for command in commands
                if command.get("domain") == "stock_index" and day in [str(arg) for arg in (command.get("cmd") or [])]
            ]
            mark_known_bad_session(
                con,
                day,
                reason="bounded_repair_failed_for_observed_bad_session",
                source_errors="\n".join(errors),
            )
            marked.append(day)
    finally:
        con.close()
    return marked


def _emit(logs: list[str], message: str) -> None:
    logs.append(message)
    print(message, flush=True)


def _missing_window_size(expected_dates: list[str], missing_days: list[str]) -> int:
    if not missing_days:
        return 0
    positions = {day: idx for idx, day in enumerate(expected_dates)}
    found = [positions[day] for day in missing_days if day in positions]
    if not found:
        return max(1, len(missing_days))
    return max(1, max(found) - min(found) + 1)


def _ensure_kline_schema(con: sqlite3.Connection) -> None:
    con.execute(
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
    con.commit()


def _upsert_kline_rows(con: sqlite3.Connection, rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    placeholders = ",".join(["?"] * len(KLINE_COLS))
    sql = f"INSERT OR REPLACE INTO kline_daily ({','.join(KLINE_COLS)}) VALUES ({placeholders})"
    payload = [tuple(row.get(col) for col in KLINE_COLS) for row in rows]
    con.executemany(sql, payload)
    con.commit()
    return len(payload)


def _pick_column(columns: Iterable[Any], names: Iterable[str]) -> str | None:
    cols = [str(col) for col in columns]
    lower = {col.lower(): col for col in cols}
    for name in names:
        if name.lower() in lower:
            return lower[name.lower()]
    return None


def _to_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except Exception:
        return None


def _akshare_hist_row_to_kline(row: Any, sec_type: str, sec_code: str, day: str) -> dict[str, Any] | None:
    try:
        columns = list(getattr(row, "index", []))
        date_col = _pick_column(columns, ("日期", "date", "trade_date"))
        close_col = _pick_column(columns, ("收盘", "close"))
        if not date_col or not close_col:
            return None
        row_day = normalize_day(row.get(date_col))
        if row_day != day:
            return None
        close = _to_float(row.get(close_col))
        pre_close = _to_float(row.get(_pick_column(columns, ("昨收", "pre_close", "preclose")) or ""))
        change = _to_float(row.get(_pick_column(columns, ("涨跌额", "change")) or ""))
        change_pct = _to_float(row.get(_pick_column(columns, ("涨跌幅", "change_pct", "pctChg")) or ""))
        if pre_close is None and close is not None and change is not None:
            pre_close = close - change
        return {
            "sec_type": sec_type,
            "sec_code": _canonical_code(sec_code),
            "trade_date": day,
            "open": _to_float(row.get(_pick_column(columns, ("开盘", "open")) or "")),
            "high": _to_float(row.get(_pick_column(columns, ("最高", "high")) or "")),
            "low": _to_float(row.get(_pick_column(columns, ("最低", "low")) or "")),
            "close": close,
            "pre_close": pre_close,
            "change": change,
            "change_pct": change_pct,
            "volume": _to_float(row.get(_pick_column(columns, ("成交量", "volume")) or "")),
            "amount": _to_float(row.get(_pick_column(columns, ("成交额", "amount")) or "")),
            "turnover_ratio": _to_float(row.get(_pick_column(columns, ("换手率", "turnover_ratio", "turn")) or "")),
            "source": "akshare",
            "updated_at": dt.datetime.now().isoformat(timespec="seconds"),
        }
    except Exception:
        return None


def _stock_codes_for_fallback(con: sqlite3.Connection, limit: int | None = None) -> list[str]:
    queries = [
        "SELECT DISTINCT sec_code FROM stock_info WHERE sec_code IS NOT NULL ORDER BY sec_code",
        "SELECT DISTINCT sec_code FROM kline_daily WHERE sec_type='stock' AND sec_code IS NOT NULL ORDER BY sec_code",
    ]
    for sql in queries:
        try:
            rows = con.execute(sql).fetchall()
        except Exception:
            continue
        codes = [_canonical_code(row[0]) for row in rows if row and row[0]]
        codes = [code for code in codes if code]
        if codes:
            return codes[:limit] if limit else codes
    return []


def _flush_akshare_rows(con: sqlite3.Connection, rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    written = _upsert_kline_rows(con, rows)
    con.commit()
    rows.clear()
    return written



def _max_date_from_table(
    db_path: str | Path,
    table: str,
    date_column: str,
    where_sql: str = "",
    params: tuple[Any, ...] = (),
) -> str | None:
    if not Path(db_path).exists():
        return None
    con = _connect(db_path)
    try:
        if not _table_exists(con, table):
            return None
        where = f"WHERE {where_sql}" if where_sql else ""
        value = _scalar(
            con,
            f"SELECT MAX(substr(replace({date_column}, '/', '-'), 1, 10)) FROM {table} {where}",
            params,
        )
        return normalize_day(value)
    except Exception:
        return None
    finally:
        con.close()


def _stock_trade_calendar_for_health(stock_db: str | Path) -> list[str]:
    if not Path(stock_db).exists():
        return []
    con = _connect(stock_db)
    try:
        if not _table_exists(con, "kline_daily"):
            return []
        rows = con.execute(
            """
            SELECT DISTINCT substr(replace(trade_date, '/', '-'), 1, 10) AS trade_date
            FROM kline_daily
            WHERE sec_type='stock'
            ORDER BY trade_date
            """
        ).fetchall()
        return [day for day in (normalize_day(row[0]) for row in rows) if day]
    except Exception:
        return []
    finally:
        con.close()


def _lag_vs_stock(calendar: list[str], stock_max_date: str | None, domain_date: str | None) -> int | None:
    if not calendar or not stock_max_date or not domain_date:
        return None
    if domain_date >= stock_max_date:
        return 0
    return sum(1 for day in calendar if domain_date < day <= stock_max_date)


def _etf_effective_max_date(etf_db: str | Path) -> str | None:
    scale = _max_date_from_table(etf_db, "etf_scale", "trade_date")
    total = _max_date_from_table(etf_db, "etf_total", "trade_date")
    dates = [day for day in (scale, total) if day]
    if len(dates) == 2:
        return min(dates)
    return dates[0] if dates else None


def _status_distribution(db_path: str | Path, table: str) -> tuple[dict[str, int], int]:
    if not Path(db_path).exists():
        return {}, 0
    con = _connect(db_path)
    try:
        if not _table_exists(con, table):
            return {}, 0
        rows = con.execute(
            f"SELECT COALESCE(status, 'n/a') AS status, COUNT(*) FROM {table} GROUP BY COALESCE(status, 'n/a')"
        ).fetchall()
        distribution = {str(row[0]): int(row[1]) for row in rows}
        return distribution, int(distribution.get("complete", 0))
    except Exception:
        return {}, 0
    finally:
        con.close()


def _count_rows_for_day(db_path: str | Path, table: str, date_column: str, target_date: str | None) -> int | None:
    if not target_date or not Path(db_path).exists():
        return None
    con = _connect(db_path)
    try:
        if not _table_exists(con, table):
            return None
        return int(
            _scalar(
                con,
                f"SELECT COUNT(*) FROM {table} WHERE substr(replace({date_column}, '/', '-'), 1, 10)=?",
                (target_date,),
            )
            or 0
        )
    except Exception:
        return None
    finally:
        con.close()


def _health_errors(update_result: dict[str, Any] | None) -> list[str]:
    if not update_result:
        return []
    errors: list[str] = []
    for command in update_result.get("commands", []) or []:
        rc = int(command.get("returncode") or 0)
        if rc != 0:
            output = str(command.get("output") or "").strip().splitlines()
            detail = output[-1] if output else "no output"
            errors.append(f"{command.get('domain', 'unknown')} rc={rc}: {detail[:300]}")
        output_text = str(command.get("output") or "")
        for line in output_text.splitlines():
            if "watchlist_validation" in line and "error" in line:
                errors.append(f"watchlist_validation.error: {line[:300]}")
    for line in update_result.get("logs", []) or []:
        text = str(line)
        if " not found" in text or " error=" in text and not text.endswith("error=None"):
            errors.append(text[:300])
    return errors


def write_health_summary(base_dir: str | Path, update_result: dict[str, Any] | None = None) -> dict[str, Any]:
    paths = build_runtime_paths(str(base_dir))
    base = Path(paths.base_dir)
    output_dir = base / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    state_path = output_dir / "health_state.json"
    health_path = output_dir / "health_latest.md"
    mining_db = base / "mining_mvp.db"

    calendar = _stock_trade_calendar_for_health(paths.stock_db)
    stock_max = calendar[-1] if calendar else _max_date_from_table(paths.stock_db, "kline_daily", "trade_date", "sec_type='stock'")
    plan_dates = ((update_result or {}).get("plan") or {}).get("expected_dates") or []
    target_date = normalize_day(plan_dates[-1]) if plan_dates else stock_max

    domain_dates = {
        "stock": stock_max,
        "index": _max_date_from_table(paths.stock_db, "kline_daily", "trade_date", "sec_type='index'"),
        "concept": _max_date_from_table(paths.concept_db, "concept_kline", "trade_date")
        or _max_date_from_table(paths.concept_db, "concept_kline_ths", "trade_date"),
        "etf": _etf_effective_max_date(paths.etf_db),
        "mining candidates": _max_date_from_table(mining_db, "candidates", "trade_date"),
        "watchlist_snapshots": _max_date_from_table(mining_db, "watchlist_snapshots", "snapshot_date"),
    }
    freshness = {
        domain: {
            "max_date": day,
            "lag_vs_stock_days": _lag_vs_stock(calendar, stock_max, day),
        }
        for domain, day in domain_dates.items()
    }

    today_rows = {
        "candidates": _count_rows_for_day(mining_db, "candidates", "trade_date", target_date),
        "watchlist_snapshots": _count_rows_for_day(mining_db, "watchlist_snapshots", "snapshot_date", target_date),
    }
    outcome_status, outcome_complete = _status_distribution(mining_db, "outcomes")
    snapshot_status, snapshot_complete = _status_distribution(mining_db, "watchlist_outcomes")

    previous: dict[str, Any] = {}
    if state_path.exists():
        try:
            previous = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:
            previous = {}
    previous_complete = previous.get("complete_counts") or {}
    previous_streaks = previous.get("zero_complete_delta_streaks") or {}
    complete_counts = {"outcomes": outcome_complete, "watchlist_outcomes": snapshot_complete}
    complete_delta = {
        table: int(count) - int(previous_complete.get(table, 0))
        for table, count in complete_counts.items()
    }
    zero_streaks = {
        table: (int(previous_streaks.get(table, 0)) + 1 if delta == 0 else 0)
        for table, delta in complete_delta.items()
    }

    errors = _health_errors(update_result)
    alerts: list[str] = []
    for domain, info in freshness.items():
        lag = info.get("lag_vs_stock_days")
        if lag is not None and lag > 2:
            alerts.append(f"[ALERT] {domain} lag_vs_stock_days={lag} max_date={info.get('max_date') or 'n/a'}")
    for table, streak in zero_streaks.items():
        if streak >= 3:
            alerts.append(f"[ALERT] {table} complete_delta_zero_streak={streak}")
    if errors:
        alerts.append(f"[ALERT] domain_errors={len(errors)}")

    generated_at = dt.datetime.now().isoformat(timespec="seconds")
    lines = [
        "# Daily Health",
        "",
        f"generated_at: {generated_at}",
        f"target_date: {target_date or 'n/a'}",
        f"update_ok: {bool((update_result or {}).get('ok')) if update_result is not None else 'n/a'}",
        "",
        "## Data Freshness",
        "",
        "| domain | max_date | lag_vs_stock_days |",
        "| --- | --- | ---: |",
    ]
    for domain, info in freshness.items():
        lag = info.get("lag_vs_stock_days")
        lines.append(f"| {domain} | {info.get('max_date') or 'n/a'} | {lag if lag is not None else 'n/a'} |")
    lines.extend(
        [
            "",
            "## Today Writes",
            "",
            f"- candidates: {today_rows['candidates'] if today_rows['candidates'] is not None else 'n/a'}",
            f"- watchlist_snapshots: {today_rows['watchlist_snapshots'] if today_rows['watchlist_snapshots'] is not None else 'n/a'}",
            "",
            "## Outcome Status",
            "",
            f"- outcomes: {outcome_status or 'n/a'}; complete_delta={complete_delta['outcomes']}",
            f"- watchlist_outcomes: {snapshot_status or 'n/a'}; complete_delta={complete_delta['watchlist_outcomes']}",
            "",
            "## Errors",
            "",
        ]
    )
    lines.extend([f"- {error}" for error in errors] or ["- none"])
    lines.extend(["", "## Alerts", ""])
    lines.extend(alerts or ["- none"])
    health_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    state = {
        "generated_at": generated_at,
        "target_date": target_date,
        "complete_counts": complete_counts,
        "zero_complete_delta_streaks": zero_streaks,
        "health_path": str(health_path),
    }
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return {
        "path": str(health_path),
        "state_path": str(state_path),
        "target_date": target_date,
        "freshness": freshness,
        "today_rows": today_rows,
        "outcome_status": {"outcomes": outcome_status, "watchlist_outcomes": snapshot_status},
        "complete_delta": complete_delta,
        "alerts": alerts,
        "errors": errors,
    }


def _append_push_status(health_path: str | Path | None, push_result: dict[str, Any]) -> None:
    if not health_path:
        return
    path = Path(health_path)
    if not path.exists():
        return
    status = str(push_result.get("status") or "unknown")
    reason = push_result.get("reason") or push_result.get("error")
    detail = f"{status}: {reason}" if reason else status
    body = path.read_text(encoding="utf-8").rstrip()
    marker = "\n## Push\n"
    if marker in body:
        body = body.split(marker, 1)[0].rstrip()
    path.write_text(f"{body}\n\n## Push\n\n- push: {detail}\n", encoding="utf-8")


def _send_daily_push(base_dir: str | Path, health: dict[str, Any]) -> dict[str, Any]:
    trade_date = health.get("target_date")
    if not trade_date:
        result = {"status": "skipped", "reason": "no target_date"}
        _append_push_status(health.get("path"), result)
        return result
    try:
        from mining.db import connect
        from mining.notify import send_daily_digest

        conn = connect(base_dir=base_dir)
        try:
            result = send_daily_digest(conn, str(trade_date))
        finally:
            conn.close()
    except Exception as exc:
        result = {"status": "failed", "error": str(exc)}
    _append_push_status(health.get("path"), result)
    return result


def akshare_stock_index_backfill(
    stock_db: str | Path,
    day: str,
    *,
    ak_module: Any = None,
    max_failures: int = MAX_INTERFACE_ATTEMPTS,
    stock_limit: int | None = None,
    request_timeout: float = 10.0,
    batch_size: int = 200,
) -> dict[str, Any]:
    target = normalize_day(day)
    if target is None:
        return {"ok": False, "rows": 0, "attempts": 0, "error": f"invalid day: {day!r}"}
    if ak_module is None:
        try:
            import akshare as ak_module  # type: ignore
        except Exception as exc:
            return {"ok": False, "rows": 0, "attempts": 0, "error": f"AkShare import failed: {exc}"}

    ymd = target.replace("-", "")
    failures = 0
    attempts = 0
    written = 0
    rows: list[dict[str, Any]] = []

    con = _connect(stock_db)
    try:
        _ensure_kline_schema(con)
        stock_codes = _stock_codes_for_fallback(con, limit=stock_limit)
        if not stock_codes:
            return {"ok": False, "rows": 0, "attempts": 0, "error": "no stock codes available for AkShare fallback"}

        probe_codes = [code for code in ("600000", "000001", "300750", "600519", "000333") if code in set(stock_codes)]
        if not probe_codes:
            probe_codes = stock_codes[: min(5, len(stock_codes))]
        probe_rows: list[dict[str, Any]] = []
        probe_errors: list[str] = []
        for code in probe_codes:
            try:
                attempts += 1
                df = ak_module.stock_zh_a_hist(
                    symbol=code,
                    period="daily",
                    start_date=ymd,
                    end_date=ymd,
                    adjust="",
                    timeout=request_timeout,
                )
                if df is None or getattr(df, "empty", True):
                    probe_errors.append(f"{code}: empty")
                    continue
                row = _akshare_hist_row_to_kline(df.iloc[-1], "stock", code, target)
                if row is None:
                    probe_errors.append(f"{code}: no target row")
                else:
                    probe_rows.append(row)
            except Exception as exc:
                probe_errors.append(f"{code}: {exc}")
            if probe_rows:
                break
        if not probe_rows:
            return {
                "ok": False,
                "rows": 0,
                "attempts": attempts,
                "error": f"AkShare stock probe failed for {target}: {'; '.join(probe_errors[-5:])}",
            }
        rows.extend(probe_rows)
        probed = {row["sec_code"] for row in probe_rows}

        for code in stock_codes:
            if code in probed:
                continue
            try:
                attempts += 1
                df = ak_module.stock_zh_a_hist(
                    symbol=code,
                    period="daily",
                    start_date=ymd,
                    end_date=ymd,
                    adjust="",
                    timeout=request_timeout,
                )
                if df is None or getattr(df, "empty", True):
                    continue
                row = _akshare_hist_row_to_kline(df.iloc[-1], "stock", code, target)
                if row is None:
                    failures += 1
                else:
                    rows.append(row)
                    failures = 0
            except Exception as exc:
                failures += 1
                if failures >= int(max_failures):
                    return {
                        "ok": False,
                        "rows": 0,
                        "attempts": attempts,
                        "error": f"AkShare failed 3 times while fetching stocks: {exc}",
                    }
            if failures >= int(max_failures):
                written += _flush_akshare_rows(con, rows)
                return {
                    "ok": False,
                    "rows": written,
                    "attempts": attempts,
                    "error": "AkShare failed 3 times while extracting stock rows",
                }
            if len(rows) >= max(1, int(batch_size)):
                written += _flush_akshare_rows(con, rows)

        for code in REQUIRED_INDEX_CODES:
            try:
                attempts += 1
                df = ak_module.index_zh_a_hist(
                    symbol=code,
                    period="daily",
                    start_date=ymd,
                    end_date=ymd,
                )
                if df is None or getattr(df, "empty", True):
                    failures += 1
                else:
                    row = _akshare_hist_row_to_kline(df.iloc[-1], "index", code, target)
                    if row is None:
                        failures += 1
                    else:
                        rows.append(row)
                        failures = 0
            except Exception as exc:
                failures += 1
                if failures >= int(max_failures):
                    written += _flush_akshare_rows(con, rows)
                    return {
                        "ok": False,
                        "rows": written,
                        "attempts": attempts,
                        "error": f"AkShare failed 3 times while fetching indexes: {exc}",
                    }
            if failures >= int(max_failures):
                written += _flush_akshare_rows(con, rows)
                return {
                    "ok": False,
                    "rows": written,
                    "attempts": attempts,
                    "error": "AkShare failed 3 times while extracting index rows",
                }

        written += _flush_akshare_rows(con, rows)
        coverage = stock_coverage_for_date(stock_db, target)
        ok = bool(coverage.get("stock")) and bool(coverage.get("index"))
        error = None if ok else f"AkShare wrote {written} rows but coverage is incomplete: {coverage}"
        return {"ok": ok, "rows": written, "attempts": attempts, "coverage": coverage, "error": error}
    finally:
        con.close()


def run_offline_update(
    base_dir: str | Path,
    *,
    asof: str | None = None,
    days: int = 10,
    dry_run: bool = False,
    timeout_sec: int = 600,
    use_remote_calendar: bool = False,
) -> dict[str, Any]:
    paths = build_runtime_paths(str(base_dir))
    expected_dates = resolve_expected_trade_days(
        paths.stock_db,
        asof=asof,
        days=days,
        use_baostock=bool(use_remote_calendar),
    )
    plan = build_missing_update_plan(paths.base_dir, expected_dates)
    logs: list[str] = []
    _emit(logs, f"expected_dates={expected_dates}")
    _emit(logs, f"missing_by_day={plan.get('missing_by_day')}")
    _emit(logs, f"missing_by_domain={plan.get('missing_by_domain')}")
    if dry_run or plan.get("ok"):
        result = {"ok": bool(plan.get("ok")), "plan": plan, "logs": logs, "commands": []}
        result["health"] = write_health_summary(paths.base_dir, result)
        result["push"] = _send_daily_push(paths.base_dir, result["health"])
        return result

    commands: list[dict[str, Any]] = []

    market_days = _domain_days(plan, "stock", "index")
    if market_days:
        repair_script = _find_script(paths.base_dir, SCRIPT_REPAIR_MARKET_DAY_CANDIDATES)
        if repair_script is not None:
            for market_day in market_days:
                cmd = [
                    sys.executable,
                    str(repair_script),
                    "--db",
                    paths.stock_db,
                    "--date",
                    market_day,
                    "--workers",
                    "12",
                    "--attempts-per-source",
                    "1",
                ]
                _emit(logs, f"stock_index repair start asof={market_day}")
                rc, out = _run(cmd, cwd=paths.base_dir, timeout_sec=max(30, timeout_sec))
                commands.append(
                    {
                        "domain": "stock_index",
                        "interface": "repair_market_day_akshare",
                        "cmd": cmd,
                        "returncode": rc,
                        "attempts": 1,
                        "output": out[-4000:],
                    }
                )
                coverage = stock_coverage_for_date(paths.stock_db, market_day)
                _emit(logs, f"stock_index repair rc={rc} asof={market_day} coverage={coverage}")
        else:
            for market_day in market_days:
                _emit(logs, f"stock_index akshare start asof={market_day}")
                fallback = akshare_stock_index_backfill(paths.stock_db, market_day)
                commands.append(
                    {
                        "domain": "stock_index",
                        "interface": "akshare",
                        "cmd": ["akshare_stock_index_backfill", paths.stock_db, market_day],
                        "returncode": 0 if fallback.get("ok") else 999,
                        "attempts": fallback.get("attempts"),
                        "output": str(fallback),
                    }
                )
                _emit(
                    logs,
                    f"stock_index akshare ok={fallback.get('ok')} attempts={fallback.get('attempts')} "
                    f"rows={fallback.get('rows')} asof={market_day} error={fallback.get('error')}"
                )

        current_plan = build_missing_update_plan(paths.base_dir, expected_dates)
        market_days = _domain_days(current_plan, "stock", "index")

    if market_days:
        _emit(logs, f"skip baostock fallback for unresolved stock/index days {market_days}; use AkShare/repair source explicitly")

    current_plan = build_missing_update_plan(paths.base_dir, expected_dates)

    concept_days = _domain_days(current_plan, "concept")
    if concept_days:
        script = _find_script(paths.base_dir, SCRIPT_CONCEPT_CANDIDATES)
        if script is None:
            _emit(logs, "concept script not found")
        else:
            for concept_attempt, asof_arg in enumerate(
                (concept_days[-1].replace("-", ""), concept_days[-1], concept_days[-1].replace("-", "/")),
                start=1,
            ):
                cmd = [
                    sys.executable,
                    str(script),
                    "--db",
                    paths.concept_db,
                    "--days",
                    str(max(days, len(concept_days) + 5)),
                    "--asof",
                    asof_arg,
                ]
                rc, out = _run(cmd, cwd=paths.base_dir, timeout_sec=timeout_sec)
                commands.append({"domain": "concept", "cmd": cmd, "returncode": rc, "attempts": 1, "output": out[-4000:]})
                _emit(logs, f"concept rc={rc} attempt={concept_attempt}/3 asof={asof_arg}")
                if rc == 0:
                    break

    current_plan = build_missing_update_plan(paths.base_dir, expected_dates)

    etf_days = _domain_days(current_plan, "etf")
    blocked_market_days = set(_domain_days(current_plan, "stock", "index"))
    blocked_etf_days = sorted(set(etf_days) & blocked_market_days)
    etf_days = [day for day in etf_days if day not in blocked_market_days]
    if blocked_etf_days:
        _emit(logs, f"skip etf until stock/index is available for {blocked_etf_days}")
    if etf_days:
        script = _find_script(paths.base_dir, SCRIPT_ETF_CANDIDATES)
        if script is None:
            _emit(logs, "etf script not found")
        else:
            cmd = [
                sys.executable,
                str(script),
                "--db",
                paths.etf_db,
                "--days",
                str(max(days, len(etf_days) + 5)),
                "--asof",
                etf_days[-1],
                "--ref-db",
                paths.stock_db,
            ]
            rc, out, attempts = _run_with_retries(cmd, cwd=paths.base_dir, timeout_sec=timeout_sec)
            commands.append({"domain": "etf", "cmd": cmd, "returncode": rc, "attempts": attempts, "output": out[-4000:]})
            _emit(logs, f"etf rc={rc} attempts={attempts} asof={etf_days[-1]}")

    post_market_plan = build_missing_update_plan(paths.base_dir, expected_dates)
    mining_days = sorted(
        set(_domain_days(plan, "mining"))
        | set(day for day, missing in (post_market_plan.get("missing_by_day") or {}).items() if "mining" in missing)
    )
    blocked_market_days = set(_domain_days(post_market_plan, "stock", "index"))
    blocked_mining_days = sorted(set(mining_days) & blocked_market_days)
    mining_days = [day for day in mining_days if day not in blocked_market_days]
    if blocked_mining_days:
        _emit(logs, f"skip mining until stock/index is available for {blocked_mining_days}")
    if mining_days:
        script = _find_script(paths.base_dir, ("run_daily.py",))
        if script is None:
            _emit(logs, "run_daily.py not found")
        else:
            cmd = [
                sys.executable,
                str(script),
                "--base-dir",
                paths.base_dir,
                "--range",
                mining_days[0],
                mining_days[-1],
            ]
            rc, out, attempts = _run_with_retries(cmd, cwd=paths.base_dir, timeout_sec=timeout_sec)
            commands.append({"domain": "mining", "cmd": cmd, "returncode": rc, "attempts": attempts, "output": out[-4000:]})
            _emit(logs, f"mining rc={rc} attempts={attempts} range={mining_days[0]}..{mining_days[-1]}")

    final_plan = build_missing_update_plan(paths.base_dir, expected_dates)
    unresolved_bad_days = [
        day
        for day in _domain_days(final_plan, "stock")
        if (final_plan.get("coverage", {}).get(day, {}).get("session_quality", {}).get("status") == STATUS_BAD)
    ]
    marked_bad_days = _mark_unrecoverable_bad_stock_sessions(paths.stock_db, unresolved_bad_days, commands)
    if marked_bad_days:
        _emit(logs, f"known_bad_sessions={marked_bad_days}")
        final_plan = build_missing_update_plan(paths.base_dir, expected_dates)
    result = {"ok": bool(final_plan.get("ok")), "plan": final_plan, "initial_plan": plan, "logs": logs, "commands": commands}
    result["health"] = write_health_summary(paths.base_dir, result)
    result["push"] = _send_daily_push(paths.base_dir, result["health"])
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Check missing local daily data and run daily update scripts.")
    parser.add_argument("--base-dir", default=str(Path.cwd()))
    parser.add_argument("--asof", default=None)
    parser.add_argument("--days", type=int, default=10)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--timeout-sec", type=int, default=600)
    parser.add_argument("--use-remote-calendar", action="store_true")
    args = parser.parse_args()

    result = run_offline_update(
        args.base_dir,
        asof=args.asof,
        days=max(1, int(args.days)),
        dry_run=bool(args.dry_run),
        timeout_sec=max(30, int(args.timeout_sec)),
        use_remote_calendar=bool(args.use_remote_calendar),
    )
    for line in result.get("logs", []):
        print(line)
    for command in result.get("commands", []):
        print(f"[{command.get('domain')}] rc={command.get('returncode')} cmd={' '.join(command.get('cmd') or [])}")
        output = command.get("output")
        if output:
            print(output)
    health = result.get("health") or {}
    if health.get("path"):
        print(f"health={health.get('path')}")
    print(f"ok={bool(result.get('ok'))}")
    return 0 if result.get("ok") or args.dry_run else 1


if __name__ == "__main__":
    raise SystemExit(main())
