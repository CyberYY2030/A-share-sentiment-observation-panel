from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo

from mining.data_quality import (
    STATUS_BAD,
    ensure_session_diagnostics_table,
    ensure_session_revalidations_table,
    inspect_stock_session,
    mark_known_bad_session,
    raw_market_postcondition,
    reinspect_stock_session,
    revalidate_known_bad_session,
    stock_quality_is_screening_ready,
)
from mining.selection_batches import latest_complete_batch
from mining.trading_calendar import write_trade_calendar
from runtime_paths import build_runtime_paths


REQUIRED_INDEX_CODES = ("000001", "399001", "000300", "000852")
MAX_INTERFACE_ATTEMPTS = 3
CORE_UPDATE_DOMAINS = ("stock", "index")
DEFAULT_CORE_TIMEOUT_SECONDS = 3600
DEFAULT_FORMAL_RESERVE_SECONDS = 240
MIN_CHILD_TIMEOUT_SECONDS = 30
DEFAULT_MARKET_WORKERS = 1
DEFAULT_MARKET_MIN_REQUEST_INTERVAL_SEC = 0.45
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
CN_TZ = ZoneInfo("Asia/Shanghai")
CLOSE_READY_CUTOFF = dt.time(17, 30)


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


def resolve_target_close_date(
    asof: str | None = None,
    *,
    now_cn: dt.datetime | None = None,
) -> str:
    """Return the latest date eligible for close data; explicit ``asof`` can only cap it."""
    observed = now_cn or dt.datetime.now(CN_TZ)
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=CN_TZ)
    else:
        observed = observed.astimezone(CN_TZ)
    natural_cap = observed.date()
    if observed.timetz().replace(tzinfo=None) < CLOSE_READY_CUTOFF:
        natural_cap -= dt.timedelta(days=1)
    requested = normalize_day(asof) if asof else None
    if requested:
        natural_cap = min(natural_cap, dt.date.fromisoformat(requested))
    return natural_cap.isoformat()


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


def _stock_coverage_for_date(
    stock_db: str | Path,
    day: str,
    *,
    stock_min_rows: int | None = None,
    required_index_codes: Iterable[str] = REQUIRED_INDEX_CODES,
    quality_reader: Callable[[sqlite3.Connection, str], dict[str, Any]],
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

        session_quality = quality_reader(con, day)
        result.update(
            {
                "stock": (stock_min_rows is None or stock_rows >= int(stock_min_rows))
                and stock_quality_is_screening_ready(session_quality),
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
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "concept": False,
        "concept_rows": 0,
        "concept_have": 0,
        "concept_expect": None,
        "concept_missing": None,
        "concept_expect_source": "unavailable",
    }
    if not Path(concept_db).exists():
        return result

    con = _connect(concept_db)
    try:
        table = "concept_kline" if _table_exists(con, "concept_kline") else None
        if table is None and _table_exists(con, "concept_kline_ths"):
            table = "concept_kline_ths"
        if table is None:
            return result
        snapshot = None
        if _table_exists(con, "concept_coverage_expectations"):
            snapshot = con.execute(
                """
                SELECT eligible_codes_json, expect_source
                FROM concept_coverage_expectations
                WHERE trade_date=?
                """,
                (day,),
            ).fetchone()
        if snapshot is None:
            return result
        try:
            eligible_codes = {
                _canonical_code(value)
                for value in json.loads(str(snapshot[0]))
                if _canonical_code(value)
            }
        except (TypeError, ValueError, json.JSONDecodeError):
            return result
        if not eligible_codes:
            return result
        rows = {
            _canonical_code(row[0])
            for row in con.execute(
                f"""
                SELECT DISTINCT concept_code
                FROM {table}
                WHERE substr(replace(trade_date, '/', '-'), 1, 10)=?
                """,
                (day,),
            ).fetchall()
            if row and row[0] is not None
        }
        have = len(rows & eligible_codes)
        expect = len(eligible_codes)
        result.update(
            {
                "concept": have == expect,
                "concept_rows": len(rows),
                "concept_have": have,
                "concept_expect": expect,
                "concept_missing": expect - have,
                "concept_expect_source": str(snapshot[1] or "provider_eligible_universe"),
            }
        )
        # The original A.2 gate protects both the same-day expectation and the
        # latest trusted prior expectation.  Re-run it read-only here so a
        # previously shrunken expectation cannot turn an incomplete concept
        # domain green merely because every code in the smaller set is present.
        try:
            from backfill_adata_ths_concept_index_kline_60d import apply_concept_universe_gate

            universe_gate = apply_concept_universe_gate(
                con,
                [day],
                eligible_codes,
                persist=False,
            )
        except Exception as exc:
            universe_gate = {
                "ok": False,
                "reason": "eligible_universe_gate_unavailable",
                "error": f"{type(exc).__name__}: {exc}",
            }
        result["concept_universe_gate"] = universe_gate
        if not bool(universe_gate.get("ok")):
            result["concept"] = False
            result["concept_reason_codes"] = [str(universe_gate.get("reason") or "eligible_universe_gate_failed")]
        return result
    finally:
        con.close()


def etf_coverage_for_date(
    etf_db: str | Path,
    day: str,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "etf": False,
        "etf_scale_rows": 0,
        "etf_total_rows": 0,
        "etf_have": 0,
        "etf_expect": None,
        "etf_missing": None,
        "etf_expect_source": "unavailable",
    }
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
        if not (_table_exists(con, "etf_master") and _table_exists(con, "etf_scale")):
            return result
        try:
            latest_snapshot = _scalar(
                con,
                "SELECT MAX(updated_at) FROM etf_master WHERE is_equity=1 AND updated_at IS NOT NULL",
            )
            if latest_snapshot is None:
                return result
            eligible_rows = con.execute(
                "SELECT DISTINCT fund_code FROM etf_master WHERE is_equity=1 AND updated_at=?",
                (latest_snapshot,),
            ).fetchall()
        except sqlite3.OperationalError:
            # A master without a provider-snapshot marker cannot prove its denominator.
            return result
        eligible_codes = {
            _canonical_code(row[0])
            for row in eligible_rows
            if row and row[0] is not None and _canonical_code(row[0])
        }
        if not eligible_codes:
            return result
        present_codes = {
            _canonical_code(row[0])
            for row in con.execute(
                "SELECT DISTINCT fund_code FROM etf_scale WHERE substr(replace(trade_date, '/', '-'), 1, 10)=?",
                (day,),
            ).fetchall()
            if row and row[0] is not None and _canonical_code(row[0])
        }
        scale_rows = len(present_codes)
        have = len(present_codes & eligible_codes)
        expect = len(eligible_codes)
        result.update(
            {
                "etf": total_rows > 0 and have == expect,
                "etf_scale_rows": scale_rows,
                "etf_total_rows": total_rows,
                "etf_have": have,
                "etf_expect": expect,
                "etf_missing": expect - have,
                "etf_expect_source": "etf_master_latest_provider_snapshot",
                "etf_snapshot_at": str(latest_snapshot),
            }
        )
        return result
    finally:
        con.close()


def mining_coverage_for_date(base_dir: str | Path, day: str) -> dict[str, Any]:
    db_path = Path(base_dir) / "mining_mvp.db"
    result: dict[str, Any] = {
        "mining": False,
        "mining_batch_status": "not_run",
        "mining_batch_id": None,
        "mining_candidates": 0,
        "mining_runs": 0,
    }
    if not db_path.exists():
        return result

    con = _connect(db_path)
    try:
        batch = latest_complete_batch(con, day)
        result["mining_batch_status"] = "pending" if batch.code == "unfinalized" else batch.code
        result["mining_batch_id"] = batch.batch_id
        if batch.code != "complete" or batch.batch_id is None:
            return result
        candidates = 0
        runs = 0
        if _table_exists(con, "candidates") and _table_exists(con, "strategy_runs"):
            candidates = int(
                _scalar(
                    con,
                    """
                    SELECT COUNT(*) FROM candidates c
                    JOIN strategy_runs r ON r.run_id=c.run_id
                    WHERE r.batch_id=? AND r.mode='close_final'
                    """,
                    (batch.batch_id,),
                )
                or 0
            )
        if _table_exists(con, "strategy_runs"):
            runs = int(
                _scalar(
                    con,
                    "SELECT COUNT(*) FROM strategy_runs WHERE batch_id=? AND mode='close_final'",
                    (batch.batch_id,),
                )
                or 0
            )
        result.update(
            {
                "mining": True,
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
    stock_min_rows: int | None = None,
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
            )
        )
    if "etf" in selected:
        result.update(etf_coverage_for_date(paths.etf_db, normalized))
    if "mining" in selected:
        result.update(mining_coverage_for_date(paths.base_dir, normalized))
    return result


def screening_readiness_for_date(base_dir: str | Path, day: str) -> dict[str, Any]:
    """Return distinct market and formal-selection readiness for one close day."""
    normalized = normalize_day(day)
    if normalized is None:
        raise ValueError(f"Invalid day: {day!r}")
    coverage = coverage_for_date(base_dir, normalized, domains=("stock", "index", "mining"))
    quality = dict(coverage.get("session_quality") or {})
    stock_ready = bool(coverage.get("stock"))
    index_ready = bool(coverage.get("index"))
    market_data_ready = stock_ready and index_ready
    selection_ready = market_data_ready and bool(coverage.get("mining"))
    mining_status = str(coverage.get("mining_batch_status") or "not_run")
    return {
        "trade_date": normalized,
        "market_data_ready": market_data_ready,
        "market_close_ready": market_data_ready,
        "selection_ready": selection_ready,
        "screening_ready": selection_ready,
        "domains": {
            "stock": {
                "ready": stock_ready,
                "status": "ready" if stock_ready else "unavailable",
                "reason_codes": [str(value) for value in quality.get("reasons", [])],
                "quality": quality,
            },
            "index": {
                "ready": index_ready,
                "status": "ready" if index_ready else "unavailable",
                "reason_codes": [] if index_ready else ["required_index_codes_missing"],
                "index_codes": list(coverage.get("index_codes") or []),
            },
            "mining": {
                "ready": bool(coverage.get("mining")),
                "status": "complete" if coverage.get("mining") else mining_status,
                "reason_codes": [] if coverage.get("mining") else ["close_final_batch_unavailable"],
                "batch_id": coverage.get("mining_batch_id"),
                "runs": int(coverage.get("mining_runs") or 0),
                "candidates": int(coverage.get("mining_candidates") or 0),
            },
        },
    }


def _stock_quality_health_evidence(stock_db: str | Path, day: str) -> dict[str, Any]:
    """Expose day-specific quality counts without mixing quality and universe denominators."""
    coverage = stock_coverage_for_date(stock_db, day)
    quality = dict(coverage.get("session_quality") or {})
    present = int(quality.get("distinct_stock_codes", coverage.get("stock_rows", 0)) or 0)
    expected = quality.get("coverage_baseline")
    expected_count = int(expected) if expected is not None else None
    stock_info_expected: int | None = None
    if Path(stock_db).exists():
        con = _connect(stock_db)
        try:
            if _table_exists(con, "stock_info"):
                stock_info_expected = int(_scalar(con, "SELECT COUNT(DISTINCT sec_code) FROM stock_info") or 0)
        finally:
            con.close()
    selection_eligible: int | None = None
    selection_error: str | None = None
    if Path(stock_db).exists():
        memory = sqlite3.connect(":memory:")
        memory.row_factory = sqlite3.Row
        try:
            # Windows' SQLite build does not accept a mode=ro URI in ATTACH.
            # query_only makes the in-memory connection (and its attachment)
            # reject every write before the source path is attached.
            memory.execute("PRAGMA query_only = ON")
            memory.execute("ATTACH DATABASE ? AS ash", (str(Path(stock_db).resolve()),))
            from mining.selection_context import build_selection_context

            context = build_selection_context(memory, str(day))
            selection_eligible = int(context.diagnostics.get("eligible_count", len(context.universe)) or 0)
        except Exception as exc:
            selection_error = f"{type(exc).__name__}: {exc}"
        finally:
            memory.close()
    return {
        "valid": int(quality.get("valid_trade_rows", 0) or 0),
        "present": present,
        "expected": expected_count,
        "quarantined": int(quality.get("isolated_rows", 0) or 0),
        "absent": max(expected_count - present, 0) if expected_count is not None else None,
        "universe_expected": stock_info_expected,
        "universe_absent": max(stock_info_expected - present, 0) if stock_info_expected is not None else None,
        "selection_eligible": selection_eligible,
        "selection_error": selection_error,
    }


def readiness_for_date(
    base_dir: str | Path,
    day: str,
    *,
    skip_concept: bool = False,
    quality_failures: Iterable[dict[str, Any]] = (),
    include_health_evidence: bool = False,
) -> dict[str, Any]:
    """Apply the optional concept/ETF policy without contacting a provider."""
    core = screening_readiness_for_date(base_dir, day)
    normalized = str(core["trade_date"])
    optional_domains = ("etf",) if skip_concept else ("concept", "etf")
    coverage = coverage_for_date(base_dir, normalized, domains=optional_domains)
    domains = dict(core["domains"])
    failure_codes = [str(item.get("code")) for item in quality_failures if item.get("code")]
    concept_reasons = list(coverage.get("concept_reason_codes") or [])
    if "eligible_universe_regression" in failure_codes and "eligible_universe_regression" not in concept_reasons:
        concept_reasons.append("eligible_universe_regression")
    if skip_concept:
        domains["concept"] = {
            "ready": False,
            "status": "optional_skipped",
            "reason_codes": ["optional_skipped"],
        }
    else:
        concept_ready = bool(coverage.get("concept")) and not concept_reasons
        domains["concept"] = {
            "ready": concept_ready,
            "status": "ready" if concept_ready else "unavailable",
            "reason_codes": concept_reasons or ([] if concept_ready else ["concept_coverage_incomplete"]),
            "coverage": {
                key: coverage.get(key)
                for key in ("concept_rows", "concept_have", "concept_expect", "concept_missing", "concept_expect_source")
            },
        }
    etf_ready = bool(coverage.get("etf"))
    domains["etf"] = {
        "ready": etf_ready,
        "status": "ready" if etf_ready else "unavailable",
        "reason_codes": [] if etf_ready else ["etf_coverage_incomplete"],
    }
    optional_degraded = any(not bool(domains[name]["ready"]) for name in ("concept", "etf"))
    if not core["market_data_ready"]:
        overall_status = "blocked"
    elif not core["selection_ready"]:
        overall_status = "pending"
    else:
        overall_status = "degraded" if optional_degraded else "ready"
    result = {
        **core,
        "domains": domains,
        "overall_status": overall_status,
    }
    if include_health_evidence:
        paths = build_runtime_paths(str(base_dir))
        result["stock_quality"] = _stock_quality_health_evidence(paths.stock_db, normalized)
    return result


def build_missing_update_plan(
    base_dir: str | Path,
    expected_dates: Iterable[Any],
    *,
    domains: Iterable[str] | None = None,
    stock_min_rows: int | None = None,
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


def _refresh_local_trade_calendar(base_dir: str | Path, *, asof: str) -> dict[str, str]:
    """Persist only provider-confirmed A-share open dates for panel read-only use."""
    provider_error_types: list[str] = []
    for source, loader in (("akshare", _akshare_trade_days), ("baostock", _baostock_trade_days)):
        try:
            dates = loader(asof, 10000)
        except Exception as exc:
            provider_error_types.append(f"{source}:{type(exc).__name__}")
            continue
        if dates:
            payload = write_trade_calendar(base_dir, source=source, open_dates=dates, coverage_end=asof)
            return {"status": "updated", "source": source, "coverage_end": str(payload["coverage_end"])}
    retained = {"status": "retained", "reason": "provider_calendar_unavailable"}
    if provider_error_types:
        retained["provider_error_types"] = ",".join(provider_error_types)
    return retained


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


def _remaining_budget_seconds(deadline: float, *, reserve_seconds: int = 0) -> int:
    """Return this child-process allowance from one invocation-wide deadline."""
    remaining = int(deadline - time.monotonic())
    return max(0, remaining - max(0, int(reserve_seconds)))


def _run_with_remaining_budget(
    cmd: list[str],
    *,
    cwd: str | Path,
    deadline: float,
    reserve_seconds: int = 0,
    runner: Any = _run,
) -> tuple[int, str, int]:
    allowance = _remaining_budget_seconds(deadline, reserve_seconds=reserve_seconds)
    if allowance < 30:
        return 998, "invocation deadline exhausted before child launch", allowance
    rc, output = runner(cmd, cwd=cwd, timeout_sec=allowance)
    return int(rc), output, allowance


def _run_child_with_remaining_budget(
    cmd: list[str],
    *,
    cwd: str | Path,
    deadline: float,
    reserve_seconds: int = 0,
    runner: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Run one core child and preserve whether the operating system launched it.

    ``returncode`` is deliberately insufficient for the scheduler: a missing
    executable and a launched child with a non-zero exit need different
    recovery behaviour.  Test runners may return the same ``(rc, output)``
    tuple as the legacy helper, or a fully-structured mapping.
    """
    allowance = _remaining_budget_seconds(deadline, reserve_seconds=reserve_seconds)
    result: dict[str, Any] = {
        "started": False,
        "returncode": None,
        "timed_out": False,
        "error_kind": None,
        "output": "",
        "allowance": allowance,
    }
    if allowance < MIN_CHILD_TIMEOUT_SECONDS:
        result.update(
            error_kind="budget_exhausted",
            output="invocation deadline exhausted before child launch",
        )
        return result

    try:
        if runner is None:
            proc = subprocess.run(
                cmd,
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=allowance,
            )
            output = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
            result.update(started=True, returncode=int(proc.returncode), output=output.strip())
            return result

        observed = runner(cmd, cwd=cwd, timeout_sec=allowance)
        if isinstance(observed, dict):
            result.update(observed)
            result["allowance"] = allowance
            result["started"] = bool(result.get("started"))
            return result
        returncode, output = observed
        result.update(started=True, returncode=int(returncode), output=str(output or ""))
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout or ""
        stderr = exc.stderr or ""
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        result.update(started=True, timed_out=True, error_kind="timeout", output=f"{output}\n{stderr}".strip())
    except FileNotFoundError as exc:
        result.update(error_kind="file_not_found", output=str(exc))
    except PermissionError as exc:
        result.update(error_kind="permission_denied", output=str(exc))
    except OSError as exc:
        result.update(error_kind="spawn_failed", output=str(exc))
    return result


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
        if command.get("domain") == "stock_index" and bool(command.get("started"))
        for arg in (command.get("cmd") or [])
    }
    marked: list[str] = []
    if not attempted or not Path(stock_db).exists():
        return marked
    con = _connect(stock_db)
    try:
        ensure_session_diagnostics_table(con)
        for day in sorted({str(day) for day in days}):
            if day not in attempted:
                continue
            quality = reinspect_stock_session(con, day)
            if quality["status"] != STATUS_BAD:
                continue
            errors = [
                str(command.get("output") or "")[-1000:]
                for command in commands
                if command.get("domain") == "stock_index"
                and bool(command.get("started"))
                and day in [str(arg) for arg in (command.get("cmd") or [])]
            ]
            source_errors = "\n".join(errors)
            existing = con.execute(
                """
                SELECT reason, source_errors
                FROM selection_session_diagnostics
                WHERE trade_date=? AND status=?
                """,
                (day, STATUS_BAD),
            ).fetchone()
            if (
                existing is not None
                and str(existing["reason"] or "") == "bounded_repair_failed_for_observed_bad_session"
                and str(existing["source_errors"] or "") == source_errors
            ):
                continue
            con.execute("BEGIN IMMEDIATE")
            try:
                mark_known_bad_session(
                    con,
                    day,
                    reason="bounded_repair_failed_for_observed_bad_session",
                    source_errors=source_errors,
                    commit=False,
                )
                con.commit()
            except Exception:
                con.rollback()
                raise
            marked.append(day)
    finally:
        con.close()
    return marked


def stock_coverage_for_date(
    stock_db: str | Path,
    day: str,
    *,
    stock_min_rows: int | None = None,
    required_index_codes: Iterable[str] = REQUIRED_INDEX_CODES,
) -> dict[str, Any]:
    """Public coverage remains fail-closed when a diagnostic latch is present."""
    return _stock_coverage_for_date(
        stock_db,
        day,
        stock_min_rows=stock_min_rows,
        required_index_codes=required_index_codes,
        quality_reader=inspect_stock_session,
    )


def raw_stock_coverage_for_repair(
    stock_db: str | Path,
    day: str,
    *,
    stock_min_rows: int | None = None,
    required_index_codes: Iterable[str] = REQUIRED_INDEX_CODES,
) -> dict[str, Any]:
    """Repair-only coverage that deliberately re-inspects raw rows before a latch is cleared."""
    del stock_min_rows
    if not Path(stock_db).exists():
        return {"ok": False, "stock": False, "index": False, "stock_rows": 0, "index_codes": [], "session_quality": {}}
    con = _connect(stock_db)
    try:
        if not _table_exists(con, "kline_daily"):
            return {"ok": False, "stock": False, "index": False, "stock_rows": 0, "index_codes": [], "session_quality": {}}
        return raw_market_postcondition(con, day, required_index_codes=required_index_codes)
    finally:
        con.close()


def _revalidate_attempted_stock_sessions(stock_db: str | Path, days: Iterable[str]) -> list[dict[str, Any]]:
    """Re-evaluate attempted sessions without ever clearing a latch before raw checks."""
    if not Path(stock_db).exists():
        return []
    con = _connect(stock_db)
    try:
        # DDL is deliberately committed before the caller-owned BEGIN IMMEDIATE.
        ensure_session_diagnostics_table(con)
        ensure_session_revalidations_table(con)
        results: list[dict[str, Any]] = []
        for day in sorted({str(day) for day in days}):
            con.execute("BEGIN IMMEDIATE")
            try:
                result = revalidate_known_bad_session(con, day)
                con.commit()
            except Exception:
                con.rollback()
                raise
            results.append(result)
        return results
    finally:
        con.close()


def _emit(logs: list[str], message: str) -> None:
    logs.append(message)
    print(message, flush=True)


def _coverage_evidence_from_output(output: str, stage: str) -> list[dict[str, Any]]:
    prefix = f"COVERAGE {stage} "
    evidence: list[dict[str, Any]] = []
    for line in str(output or "").splitlines():
        marker = line.find(prefix)
        if marker < 0:
            continue
        try:
            value = json.loads(line[marker + len(prefix) :])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            evidence.append(value)
    return evidence


def _structured_events_from_output(output: str, event: str) -> list[dict[str, Any]]:
    prefix = f"{event} "
    events: list[dict[str, Any]] = []
    for line in str(output or "").splitlines():
        marker = line.find(prefix)
        if marker < 0:
            continue
        try:
            value = json.loads(line[marker + len(prefix) :])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            events.append(value)
    return events


def _provider_summaries_from_output(output: str) -> list[dict[str, Any]]:
    """Read the compact per-provider records emitted by the repair child."""
    prefix = "PROVIDER_SUMMARY="
    summaries: list[dict[str, Any]] = []
    for line in str(output or "").splitlines():
        marker = line.find(prefix)
        if marker < 0:
            continue
        try:
            value = json.loads(line[marker + len(prefix) :])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            summaries.append(value)
    return summaries


def _market_provider_pass_count(commands: Iterable[dict[str, Any]]) -> int:
    """Count started distinct provider passes, never parent-child launch attempts."""
    providers: set[str] = set()
    for command in commands:
        if command.get("domain") != "stock_index" or not bool(command.get("started")):
            continue
        for summary in command.get("provider_summary") or []:
            provider = str(summary.get("provider") or "")
            if provider and int(summary.get("attempted", 0)) > 0:
                providers.add(provider)
    return len(providers)


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
    readiness = (update_result or {}).get("readiness")
    if not isinstance(readiness, dict) and target_date:
        readiness = readiness_for_date(base, target_date, include_health_evidence=True)

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
    if isinstance(readiness, dict):
        stock_quality = readiness.get("stock_quality") or {}
        lines.extend(
            [
                "",
                "## Screening Readiness",
                "",
                f"- screening_ready: {bool(readiness.get('screening_ready'))}",
                f"- overall_status: {readiness.get('overall_status') or 'n/a'}",
                "- stock quality: "
                f"valid={stock_quality.get('valid', 'n/a')} "
                f"present={stock_quality.get('present', 'n/a')} "
                f"expected={stock_quality.get('expected', 'n/a')} "
                f"quarantined={stock_quality.get('quarantined', 'n/a')} "
                f"absent={stock_quality.get('absent', 'n/a')}",
                f"- stock universe: expected={stock_quality.get('universe_expected', 'n/a')} "
                f"absent={stock_quality.get('universe_absent', 'n/a')} "
                f"selection_eligible={stock_quality.get('selection_eligible', 'n/a')}",
            ]
        )
        for domain in ("stock", "index", "concept", "etf", "mining"):
            state = (readiness.get("domains") or {}).get(domain) or {}
            lines.append(
                f"- {domain}: status={state.get('status', 'n/a')} ready={state.get('ready', 'n/a')} "
                f"reasons={','.join(state.get('reason_codes') or []) or 'none'}"
            )
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
        "readiness": readiness,
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


def _core_scheduler_result(
    *,
    paths: Any,
    target_day: str,
    plan: dict[str, Any],
    logs: list[str],
    commands: list[dict[str, Any]],
    phase: str,
    reason: str,
    timeout_sec: int,
    formal_reserve_seconds: int,
    revalidations: list[dict[str, Any]] | None = None,
    quality_mismatches: list[dict[str, Any]] | None = None,
    remaining_budget_seconds: int | None = None,
    publish_health: bool,
) -> dict[str, Any]:
    """Return a structured no-launch/fail-closed core scheduler outcome."""
    readiness = readiness_for_date(
        paths.base_dir,
        target_day,
        skip_concept=False,
        include_health_evidence=True,
    )
    status_by_phase = {
        "configuration_error": ("configuration_error", "not_run"),
        "no_launch": ("no_launch", "not_run"),
        "market_running": ("market_running", "not_run"),
        "market_failed": ("market_failed", "not_run"),
        "market_ready": ("market_ready", "not_run"),
        "formal_running": ("market_ready", "formal_running"),
        "formal_failed": ("market_ready", "formal_failed"),
        "complete": ("market_ready", "complete"),
    }
    market_status, formal_status = status_by_phase.get(phase, (phase, "not_run"))
    market_attempts = _market_provider_pass_count(commands)
    formal_attempt = sum(1 for command in commands if command.get("domain") == "formal_batch")
    result = {
        "ok": False,
        "plan": plan,
        "initial_plan": plan,
        "logs": logs,
        "commands": commands,
        "target_close_date": target_day,
        "target_day": target_day,
        "domains": list(CORE_UPDATE_DOMAINS),
        "readiness": readiness,
        "scheduler_status": {
            "phase": phase,
            "reason": reason,
            "target_day": target_day,
            "market_status": market_status,
            "formal_status": formal_status,
            "market_attempts": market_attempts,
            "formal_attempt": formal_attempt,
            "terminal": phase in {"configuration_error", "no_launch", "market_failed", "formal_failed", "complete"},
            "manual_retry_required": phase in {"market_failed", "formal_failed"},
            "next_retry_at": None,
            "budget_seconds": int(timeout_sec),
            "formal_reserve_seconds": int(formal_reserve_seconds),
            "remaining_budget_seconds": (
                max(0, int(timeout_sec) - int(formal_reserve_seconds))
                if remaining_budget_seconds is None
                else max(0, int(remaining_budget_seconds))
            ),
        },
        "revalidations": list(revalidations or []),
        "quality_mismatches": list(quality_mismatches or []),
        "quality_failures": [],
        "mining_deferred": False,
        "concept_deferred": True,
    }
    if publish_health:
        result["health"] = write_health_summary(paths.base_dir, result)
        result["push"] = _send_daily_push(paths.base_dir, result["health"])
    else:
        result["health"] = {"status": "not_published"}
        result["push"] = {"status": "disabled"}
    return result


def _run_core_offline_update(
    base_dir: str | Path,
    *,
    asof: str | None,
    target_days: Iterable[str] | None,
    publish_health: bool,
    dry_run: bool,
    timeout_sec: int,
    market_workers: int,
    market_min_request_interval_sec: float,
    use_remote_calendar: bool,
    formal_reserve_seconds: int,
) -> dict[str, Any]:
    """Run the only startup writer flow: one stock/index close, then formal A-E."""
    paths = build_runtime_paths(str(base_dir))
    now_cn = dt.datetime.now(CN_TZ)
    close_cap = resolve_target_close_date(asof, now_cn=now_cn)
    explicit_targets = sorted({day for value in (target_days or []) if (day := normalize_day(value))})
    if explicit_targets:
        invalid_targets = [day for day in explicit_targets if day > close_cap]
        if invalid_targets:
            raise ValueError(f"target days are not close-ready: {invalid_targets}")
        target_day = explicit_targets[-1]
    else:
        resolved = resolve_expected_trade_days(
            paths.stock_db,
            asof=close_cap,
            days=1,
            use_baostock=bool(use_remote_calendar),
        )
        target_day = resolved[-1] if resolved else close_cap

    plan = build_missing_update_plan(paths.base_dir, [target_day], domains=CORE_UPDATE_DOMAINS)
    logs = [
        f"core_domains={list(CORE_UPDATE_DOMAINS)}",
        f"target_day={target_day}",
        f"close_cap={close_cap}",
        f"missing_by_domain={plan.get('missing_by_domain')}",
    ]
    commands: list[dict[str, Any]] = []
    reserve = int(formal_reserve_seconds)
    total = int(timeout_sec)
    if total <= 0 or reserve < 0 or reserve >= total:
        logs.append(
            "configuration_error: formal reserve must leave a positive core-child allowance "
            f"(total={total}, formal_reserve={reserve})"
        )
        return _core_scheduler_result(
            paths=paths,
            target_day=target_day,
            plan=plan,
            logs=logs,
            commands=commands,
            phase="configuration_error",
            reason="budget_reserve_exhausts_core_child",
            timeout_sec=total,
            formal_reserve_seconds=reserve,
            publish_health=publish_health,
        )

    if dry_run:
        readiness = readiness_for_date(paths.base_dir, target_day, skip_concept=False, include_health_evidence=True)
        return {
            "ok": bool(readiness["screening_ready"]),
            "plan": plan,
            "initial_plan": plan,
            "logs": logs + ["dry_run=true; no child or database write started"],
            "commands": [],
            "target_close_date": target_day,
            "target_day": target_day,
            "domains": list(CORE_UPDATE_DOMAINS),
            "readiness": readiness,
            "scheduler_status": {"phase": "dry_run", "target_day": target_day},
            "revalidations": [],
            "quality_mismatches": [],
            "quality_failures": [],
            "health": {"status": "not_published"},
            "push": {"status": "disabled"},
            "mining_deferred": False,
            "concept_deferred": True,
        }

    calendar_status = _refresh_local_trade_calendar(paths.base_dir, asof=close_cap)
    logs.append(f"a_share_trade_calendar={calendar_status}")
    deadline = time.monotonic() + total
    revalidations: list[dict[str, Any]] = []
    quality_mismatches: list[dict[str, Any]] = []
    raw_before = raw_stock_coverage_for_repair(paths.stock_db, target_day)
    raw_ready = bool(raw_before.get("stock")) and bool(raw_before.get("index"))
    market_ready = False

    if raw_ready:
        audited = _revalidate_attempted_stock_sessions(paths.stock_db, [target_day])
        revalidations.extend(audited)
        market = stock_coverage_for_date(paths.stock_db, target_day)
        market_ready = bool(market.get("stock")) and bool(market.get("index"))
        logs.append(f"core_market_revalidate target_day={target_day} ready={market_ready}")
    else:
        repair_script = _find_script(paths.base_dir, SCRIPT_REPAIR_MARKET_DAY_CANDIDATES)
        if repair_script is None:
            return _core_scheduler_result(
                paths=paths,
                target_day=target_day,
                plan=plan,
                logs=logs + ["acquisition_blocked: repair_market_day_akshare.py not found"],
                commands=commands,
                phase="configuration_error",
                reason="repair_script_missing",
                timeout_sec=total,
                formal_reserve_seconds=reserve,
                publish_health=publish_health,
            )
        # One child owns up to three *distinct provider passes* (BaoStock,
        # Sina, then a successfully probed Eastmoney).  Re-launching this
        # script three times restarts the same failing source and defeats the
        # circuit breaker, so the parent must never loop the repair command.
        cmd = [
            sys.executable,
            str(repair_script),
            "--db",
            paths.stock_db,
            "--date",
            target_day,
            "--workers",
            str(max(1, int(market_workers))),
            "--attempts-per-source",
            "1",
            "--min-request-interval-sec",
            format(max(0.0, float(market_min_request_interval_sec)), "g"),
        ]
        child = _run_child_with_remaining_budget(
            cmd,
            cwd=paths.base_dir,
            deadline=deadline,
            reserve_seconds=reserve,
        )
        child_started = bool(child.get("started"))
        returncode = child.get("returncode")
        child_output = str(child.get("output") or "")
        provider_summary = _provider_summaries_from_output(child_output)
        raw_after = raw_stock_coverage_for_repair(paths.stock_db, target_day)
        parent_ready = bool(raw_after.get("stock")) and bool(raw_after.get("index"))
        mismatch = None
        if child_started and not bool(child.get("timed_out")) and (returncode == 0) != parent_ready:
            mismatch = {
                "code": "child_parent_quality_mismatch",
                "child_ok": returncode == 0,
                "parent_ok": parent_ready,
                "returncode": returncode,
                "raw_status": (raw_after.get("session_quality") or {}).get("status"),
            }
            quality_mismatches.append(mismatch)
        commands.append(
            {
                "domain": "stock_index",
                "interface": "repair_market_day_akshare",
                "cmd": cmd,
                "started": child_started,
                "returncode": returncode,
                "timed_out": bool(child.get("timed_out")),
                "error_kind": child.get("error_kind"),
                "allowance": child.get("allowance"),
                "attempts": _market_provider_pass_count(
                    [{"domain": "stock_index", "started": child_started, "provider_summary": provider_summary}]
                ),
                "provider_summary": provider_summary,
                "raw_before_quality": raw_before,
                "raw_after_quality": raw_after,
                "quality_mismatch": mismatch,
                "output": child_output[-4000:],
            }
        )
        if not child_started:
            logs.append(
                "no_launch target_day="
                f"{target_day} error_kind={child.get('error_kind')} allowance={child.get('allowance')}"
            )
            return _core_scheduler_result(
                paths=paths,
                target_day=target_day,
                plan=plan,
                logs=logs,
                commands=commands,
                phase="no_launch",
                reason=str(child.get("error_kind") or "core_child_not_started"),
                timeout_sec=total,
                formal_reserve_seconds=reserve,
                remaining_budget_seconds=_remaining_budget_seconds(deadline, reserve_seconds=reserve),
                publish_health=publish_health,
            )
        if mismatch is not None:
            logs.append(f"child_parent_quality_mismatch target_day={target_day}")
        elif parent_ready:
            audited = _revalidate_attempted_stock_sessions(paths.stock_db, [target_day])
            revalidations.extend(audited)
            market = stock_coverage_for_date(paths.stock_db, target_day)
            market_ready = bool(market.get("stock")) and bool(market.get("index"))

        if not market_ready and not quality_mismatches:
            # This is the only path that may write an observed-bad latch: a
            # repair child actually started and the raw postcondition remained bad.
            marked = _mark_unrecoverable_bad_stock_sessions(paths.stock_db, [target_day], commands)
            if marked:
                logs.append(f"known_bad_sessions={marked}")

    readiness = readiness_for_date(paths.base_dir, target_day, skip_concept=False, include_health_evidence=True)
    market_ready = bool(readiness["market_data_ready"]) and not quality_mismatches
    if not market_ready:
        return _core_scheduler_result(
            paths=paths,
            target_day=target_day,
            plan=build_missing_update_plan(paths.base_dir, [target_day], domains=CORE_UPDATE_DOMAINS),
            logs=logs + ["acquisition_blocked: stock/index raw postcondition remains unavailable"],
            commands=commands,
            phase="market_failed",
            reason="market_close_not_ready",
            timeout_sec=total,
            formal_reserve_seconds=reserve,
            revalidations=revalidations,
            quality_mismatches=quality_mismatches,
            remaining_budget_seconds=_remaining_budget_seconds(deadline),
            publish_health=publish_health,
        )

    if not bool(readiness["selection_ready"]):
        formal_script = _find_script(paths.base_dir, ("run_daily.py",))
        if formal_script is None:
            return _core_scheduler_result(
                paths=paths,
                target_day=target_day,
                plan=build_missing_update_plan(paths.base_dir, [target_day], domains=CORE_UPDATE_DOMAINS),
                logs=logs + ["acquisition_blocked: run_daily.py not found"],
                commands=commands,
                phase="configuration_error",
                reason="formal_script_missing",
            timeout_sec=total,
            formal_reserve_seconds=reserve,
            revalidations=revalidations,
            remaining_budget_seconds=_remaining_budget_seconds(deadline),
            publish_health=publish_health,
            )
        cmd = [sys.executable, str(formal_script), "--base-dir", paths.base_dir, "--date", target_day, "--formal-only"]
        child = _run_child_with_remaining_budget(cmd, cwd=paths.base_dir, deadline=deadline)
        commands.append(
            {
                "domain": "formal_batch",
                "cmd": cmd,
                "started": bool(child.get("started")),
                "returncode": child.get("returncode"),
                "timed_out": bool(child.get("timed_out")),
                "error_kind": child.get("error_kind"),
                "allowance": child.get("allowance"),
                "output": str(child.get("output") or "")[-4000:],
            }
        )
        logs.append(
            "formal_batch target_day="
            f"{target_day} started={child.get('started')} rc={child.get('returncode')} "
            f"timed_out={child.get('timed_out')} allowance={child.get('allowance')}"
        )
        readiness = readiness_for_date(paths.base_dir, target_day, skip_concept=False, include_health_evidence=True)
        if not bool(readiness["selection_ready"]):
            if not bool(child.get("started")):
                reason = str(child.get("error_kind") or "formal_child_not_started")
            elif bool(child.get("timed_out")):
                reason = "formal_timeout"
            elif child.get("returncode") != 0:
                reason = "formal_child_failed"
            else:
                reason = "formal_postcondition_not_ready"
            return _core_scheduler_result(
                paths=paths,
                target_day=target_day,
                plan=build_missing_update_plan(paths.base_dir, [target_day], domains=CORE_UPDATE_DOMAINS),
                logs=logs,
                commands=commands,
                phase="formal_failed",
                reason=reason,
                timeout_sec=total,
                formal_reserve_seconds=reserve,
                revalidations=revalidations,
                quality_mismatches=quality_mismatches,
                remaining_budget_seconds=_remaining_budget_seconds(deadline),
                publish_health=publish_health,
            )

    result = {
        "ok": bool(readiness["screening_ready"]),
        "plan": build_missing_update_plan(paths.base_dir, [target_day], domains=CORE_UPDATE_DOMAINS),
        "initial_plan": plan,
        "logs": logs,
        "commands": commands,
        "target_close_date": target_day,
        "target_day": target_day,
        "domains": list(CORE_UPDATE_DOMAINS),
        "readiness": readiness,
        "scheduler_status": {
            "phase": "complete" if readiness["screening_ready"] else "formal_failed",
            "target_day": target_day,
            "market_status": "market_ready" if readiness["market_data_ready"] else "market_failed",
            "formal_status": "complete" if readiness["selection_ready"] else "formal_failed",
            "market_attempts": _market_provider_pass_count(commands),
            "formal_attempt": sum(1 for command in commands if command.get("domain") == "formal_batch"),
            "terminal": True,
            "manual_retry_required": not bool(readiness["screening_ready"]),
            "next_retry_at": None,
            "budget_seconds": total,
            "formal_reserve_seconds": reserve,
            "remaining_budget_seconds": _remaining_budget_seconds(deadline),
        },
        "revalidations": revalidations,
        "quality_mismatches": quality_mismatches,
        "quality_failures": [],
        "mining_deferred": False,
        "concept_deferred": True,
    }
    if publish_health:
        result["health"] = write_health_summary(paths.base_dir, result)
        result["push"] = _send_daily_push(paths.base_dir, result["health"])
    else:
        result["health"] = {"status": "not_published"}
        result["push"] = {"status": "disabled"}
    return result


def run_offline_update(
    base_dir: str | Path,
    *,
    asof: str | None = None,
    days: int = 10,
    target_days: Iterable[str] | None = None,
    include_mining: bool = True,
    include_concept: bool = True,
    publish_health: bool = True,
    dry_run: bool = False,
    timeout_sec: int = 600,
    market_workers: int = DEFAULT_MARKET_WORKERS,
    market_min_request_interval_sec: float = DEFAULT_MARKET_MIN_REQUEST_INTERVAL_SEC,
    use_remote_calendar: bool = False,
    domains: Iterable[str] | None = None,
    formal_reserve_seconds: int = DEFAULT_FORMAL_RESERVE_SECONDS,
) -> dict[str, Any]:
    selected_domains = tuple(str(domain).strip().lower() for domain in (domains or ()))
    if selected_domains:
        if tuple(selected_domains) != CORE_UPDATE_DOMAINS:
            raise ValueError(f"OPS-HARDEN-1A only permits domains {CORE_UPDATE_DOMAINS}; got {selected_domains}")
        return _run_core_offline_update(
            base_dir,
            asof=asof,
            target_days=target_days,
            publish_health=publish_health,
            dry_run=dry_run,
            timeout_sec=timeout_sec,
            market_workers=market_workers,
            market_min_request_interval_sec=market_min_request_interval_sec,
            use_remote_calendar=use_remote_calendar,
            formal_reserve_seconds=formal_reserve_seconds,
        )
    paths = build_runtime_paths(str(base_dir))
    managed_domains = _existing_domains(paths.base_dir)
    if not include_mining:
        managed_domains = [domain for domain in managed_domains if domain != "mining"]
    if not include_concept:
        managed_domains = [domain for domain in managed_domains if domain != "concept"]

    def build_plan() -> dict[str, Any]:
        return build_missing_update_plan(paths.base_dir, expected_dates, domains=managed_domains)

    now_cn = dt.datetime.now(CN_TZ)
    target_close_date = resolve_target_close_date(asof, now_cn=now_cn)
    explicit_targets = sorted({day for value in (target_days or []) if (day := normalize_day(value))})
    if explicit_targets:
        invalid_targets = [day for day in explicit_targets if day > target_close_date]
        if invalid_targets:
            raise ValueError(
                f"target days are not close-ready at {now_cn.isoformat(timespec='seconds')}: {invalid_targets}"
            )
        expected_dates = explicit_targets
    else:
        expected_dates = resolve_expected_trade_days(
            paths.stock_db,
            asof=target_close_date,
            days=days,
            use_baostock=bool(use_remote_calendar),
        )
    plan = build_plan()
    deadline = time.monotonic() + max(30, int(timeout_sec))
    market_workers = max(1, int(market_workers))
    market_min_request_interval_sec = max(0.0, float(market_min_request_interval_sec))

    def run_with_remaining_budget(cmd: list[str], *, cwd: str | Path, timeout_sec: int) -> tuple[int, str]:
        del timeout_sec
        rc, output, _allowance = _run_with_remaining_budget(cmd, cwd=cwd, deadline=deadline)
        return rc, output
    logs: list[str] = []
    _emit(
        logs,
        f"now_cn={now_cn.isoformat(timespec='seconds')} target_close_date={target_close_date} cutoff={CLOSE_READY_CUTOFF.isoformat()}",
    )
    _emit(logs, f"expected_dates={expected_dates}")
    _emit(logs, f"missing_by_day={plan.get('missing_by_day')}")
    _emit(logs, f"missing_by_domain={plan.get('missing_by_domain')}")
    if dry_run or plan.get("ok"):
        result = {
            "ok": bool(plan.get("ok")),
            "plan": plan,
            "logs": logs,
            "commands": [],
            "now_cn": now_cn.isoformat(timespec="seconds"),
            "target_close_date": target_close_date,
            "mining_deferred": not include_mining,
            "concept_deferred": not include_concept,
        }
        result["readiness"] = readiness_for_date(
            paths.base_dir,
            expected_dates[-1] if expected_dates else target_close_date,
            skip_concept=not include_concept,
            include_health_evidence=True,
        )
        if publish_health:
            result["health"] = write_health_summary(paths.base_dir, result)
            result["push"] = _send_daily_push(paths.base_dir, result["health"])
        else:
            result["health"] = {"status": "not_published"}
            result["push"] = {"status": "disabled"}
        return result

    commands: list[dict[str, Any]] = []
    revalidations: list[dict[str, Any]] = []
    quality_mismatches: list[dict[str, Any]] = []
    quality_failures: list[dict[str, Any]] = []

    revalidated_market_days: set[str] = set()

    def revalidate_market_day(day: str) -> list[dict[str, Any]]:
        """Audit a repaired date immediately so a stale latch cannot trigger another repair."""
        if day in revalidated_market_days:
            return []
        audited = _revalidate_attempted_stock_sessions(paths.stock_db, [day])
        revalidations.extend(audited)
        revalidated_market_days.add(day)
        for revalidation in audited:
            _emit(
                logs,
                "stock_index revalidate "
                f"day={revalidation['trade_date']} before_latched={revalidation['before_latched']} "
                f"raw_status={revalidation['raw']['status']} action={revalidation['action']} "
                f"after_status={revalidation['after']['status']}",
            )
        return audited

    market_days = _domain_days(plan, "stock", "index")
    if market_days:
        repair_script = _find_script(paths.base_dir, SCRIPT_REPAIR_MARKET_DAY_CANDIDATES)
        if repair_script is not None:
            later_domains = {
                domain
                for domain in ("concept", "etf", "mining")
                if _domain_days(plan, domain)
            }
            for market_position, market_day in enumerate(market_days):
                market_day_mismatch = False
                raw_before = raw_stock_coverage_for_repair(paths.stock_db, market_day)
                if bool(raw_before.get("stock")) and bool(raw_before.get("index")):
                    revalidate_market_day(market_day)
                    post_revalidation = stock_coverage_for_date(paths.stock_db, market_day)
                    if not (bool(post_revalidation.get("stock")) and bool(post_revalidation.get("index"))):
                        _emit(
                            logs,
                            f"stock_index give_up asof={market_day} reason=revalidation_not_usable "
                            f"coverage={post_revalidation}",
                        )
                    continue

                raw_after = raw_before
                # The repair child contains the bounded BaoStock/Sina/Eastmoney
                # provider pipeline.  Repeating this command would restart a
                # previously opened circuit and multiply the same interface
                # budget, so legacy callers receive the same single launch.
                for attempt in range(1, 2):
                    reserve_seconds = 120 * (
                        len(later_domains) + max(0, len(market_days) - market_position - 1)
                    )
                    cmd = [
                        sys.executable,
                        str(repair_script),
                        "--db",
                        paths.stock_db,
                        "--date",
                        market_day,
                        "--workers",
                        str(market_workers),
                        "--attempts-per-source",
                        "1",
                        "--min-request-interval-sec",
                        format(market_min_request_interval_sec, "g"),
                    ]
                    _emit(logs, f"stock_index repair start asof={market_day} attempt={attempt}")
                    rc, out, allowance = _run_with_remaining_budget(
                        cmd,
                        cwd=paths.base_dir,
                        deadline=deadline,
                        reserve_seconds=reserve_seconds,
                    )
                    raw_after = raw_stock_coverage_for_repair(paths.stock_db, market_day)
                    parent_ok = bool(raw_after.get("ok", bool(raw_after.get("stock")) and bool(raw_after.get("index"))))
                    child_ok = rc == 0
                    mismatch = None
                    if child_ok != parent_ok:
                        mismatch = {
                            "code": "child_parent_quality_mismatch",
                            "child_ok": child_ok,
                            "parent_ok": parent_ok,
                            "returncode": rc,
                            "raw_status": (raw_after.get("session_quality") or {}).get("status"),
                        }
                        quality_mismatches.append(mismatch)
                        market_day_mismatch = True
                    delta = int(raw_after.get("stock_rows", 0)) - int(raw_before.get("stock_rows", 0))
                    commands.append(
                        {
                            "domain": "stock_index",
                            "interface": "repair_market_day_akshare",
                            "cmd": cmd,
                            "returncode": rc,
                            "attempts": attempt,
                            "timeout_sec": allowance,
                            "market_workers": market_workers,
                            "market_min_request_interval_sec": market_min_request_interval_sec,
                            "raw_before_quality": raw_before,
                            "raw_after_quality": raw_after,
                            "before_stock_rows": int(raw_before.get("stock_rows", 0)),
                            "after_stock_rows": int(raw_after.get("stock_rows", 0)),
                            "stock_code_delta": delta,
                            "quality_mismatch": mismatch,
                            "output": out[-4000:],
                        }
                    )
                    _emit(
                        logs,
                        f"stock_index repair rc={rc} asof={market_day} attempt={attempt} "
                        f"stock_code_delta={delta} raw_coverage={raw_after}",
                    )
                    if mismatch is not None:
                        _emit(logs, f"child_parent_quality_mismatch asof={market_day} details={mismatch}")
                        break
                    if bool(raw_after.get("stock")) and bool(raw_after.get("index")):
                        revalidate_market_day(market_day)
                        post_revalidation = stock_coverage_for_date(paths.stock_db, market_day)
                        if not (bool(post_revalidation.get("stock")) and bool(post_revalidation.get("index"))):
                            _emit(
                                logs,
                                f"stock_index give_up asof={market_day} reason=revalidation_not_usable "
                                f"coverage={post_revalidation}",
                            )
                        break
                    if rc == 998:
                        _emit(logs, f"stock_index give_up asof={market_day} reason=invocation_deadline")
                        break
                    raw_before = raw_after
                else:
                    _emit(logs, f"stock_index give_up asof={market_day} reason=provider_pipeline_completed")

                # Retain an audit trail for a failed repair without allowing a fourth start.
                if not market_day_mismatch:
                    revalidate_market_day(market_day)
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
                revalidate_market_day(market_day)

        current_plan = build_plan()
        market_days = _domain_days(current_plan, "stock", "index")

    if market_days:
        _emit(logs, f"skip baostock fallback for unresolved stock/index days {market_days}; use AkShare/repair source explicitly")

    current_plan = build_plan()

    concept_days = _domain_days(current_plan, "concept")
    concept_coverage_evidence: list[dict[str, Any]] = []
    if concept_days:
        script = _find_script(paths.base_dir, SCRIPT_CONCEPT_CANDIDATES)
        if script is None:
            _emit(logs, "concept script not found")
        else:
            no_growth_rounds = 0
            for concept_attempt in range(1, 4):
                cmd = [
                    sys.executable,
                    str(script),
                    "--db",
                    paths.concept_db,
                    "--days",
                    str(max(days, len(concept_days) + 5)),
                    "--asof",
                    concept_days[-1],
                    "--purge_excluded",
                    "0",
                ]
                for concept_day in concept_days:
                    cmd.extend(["--target-day", concept_day])
                rc, out = run_with_remaining_budget(cmd, cwd=paths.base_dir, timeout_sec=timeout_sec)
                before_evidence = _coverage_evidence_from_output(out, "before")
                after_evidence = _coverage_evidence_from_output(out, "after")
                universe_events = _structured_events_from_output(out, "UNIVERSE_GATE")
                universe_failure = next(
                    (
                        event
                        for event in universe_events
                        if event.get("ok") is False
                        and str(event.get("reason") or "").startswith("eligible_universe_")
                    ),
                    None,
                )
                quality_failure = None
                if universe_failure is not None:
                    quality_failure = {
                        **universe_failure,
                        "code": str(universe_failure.get("reason")),
                        "domain": "concept",
                        "returncode": rc,
                    }
                    quality_failures.append(quality_failure)
                if after_evidence:
                    concept_coverage_evidence = after_evidence
                before_total = sum(int(row.get("have", 0)) for row in before_evidence)
                after_total = sum(int(row.get("have", 0)) for row in after_evidence)
                delta = after_total - before_total
                commands.append(
                    {
                        "domain": "concept",
                        "cmd": cmd,
                        "returncode": rc,
                        "attempts": concept_attempt,
                        "concept_code_delta": delta,
                        "coverage": after_evidence,
                        "quality_failure": quality_failure,
                        "output": out[-4000:],
                    }
                )
                _emit(
                    logs,
                    f"concept rc={rc} attempt={concept_attempt}/3 target_days={concept_days} "
                    f"concept_code_delta={delta} coverage={after_evidence}",
                )
                if quality_failure is not None:
                    _emit(logs, f"concept give_up reason={quality_failure['code']} details={quality_failure}")
                    break
                if after_evidence and all(int(row.get("missing", 0)) == 0 for row in after_evidence):
                    break
                if delta <= 0:
                    no_growth_rounds += 1
                else:
                    no_growth_rounds = 0
                if no_growth_rounds >= 3:
                    _emit(logs, "concept give_up reason=three_zero_growth_rounds")
                    break
                if rc == 998:
                    _emit(logs, "concept give_up reason=invocation_deadline")
                    break

    current_plan = build_plan()

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
            rc, out, attempts = _run_with_retries(
                cmd,
                cwd=paths.base_dir,
                timeout_sec=timeout_sec,
                runner=run_with_remaining_budget,
            )
            commands.append({"domain": "etf", "cmd": cmd, "returncode": rc, "attempts": attempts, "output": out[-4000:]})
            _emit(logs, f"etf rc={rc} attempts={attempts} asof={etf_days[-1]}")

    post_market_plan = build_plan()
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
            # Legacy callers are still routed through a bounded one-day formal
            # activation rather than a historical batch expansion.
            cmd = [
                sys.executable,
                str(script),
                "--base-dir",
                paths.base_dir,
                "--date",
                mining_days[-1],
                "--formal-only",
            ]
            rc, out, attempts = _run_with_retries(
                cmd,
                cwd=paths.base_dir,
                timeout_sec=timeout_sec,
                runner=run_with_remaining_budget,
            )
            commands.append({"domain": "mining", "cmd": cmd, "returncode": rc, "attempts": attempts, "output": out[-4000:]})
            _emit(logs, f"mining rc={rc} attempts={attempts} formal_day={mining_days[-1]}")

    final_plan = build_plan()
    unresolved_bad_days = [
        day
        for day in _domain_days(final_plan, "stock")
        if (final_plan.get("coverage", {}).get(day, {}).get("session_quality", {}).get("status") == STATUS_BAD)
    ]
    marked_bad_days = _mark_unrecoverable_bad_stock_sessions(paths.stock_db, unresolved_bad_days, commands)
    if marked_bad_days:
        _emit(logs, f"known_bad_sessions={marked_bad_days}")
        final_plan = build_plan()
    result = {
        "ok": bool(final_plan.get("ok")) and not quality_mismatches and not quality_failures,
        "plan": final_plan,
        "initial_plan": plan,
        "logs": logs,
        "commands": commands,
        "revalidations": revalidations,
        "quality_mismatches": quality_mismatches,
        "quality_failures": quality_failures,
        "concept_coverage": concept_coverage_evidence,
        "now_cn": now_cn.isoformat(timespec="seconds"),
        "target_close_date": target_close_date,
        "mining_deferred": not include_mining,
        "concept_deferred": not include_concept,
    }
    result["readiness"] = readiness_for_date(
        paths.base_dir,
        expected_dates[-1] if expected_dates else target_close_date,
        skip_concept=not include_concept,
        quality_failures=quality_failures,
        include_health_evidence=True,
    )
    if publish_health:
        result["health"] = write_health_summary(paths.base_dir, result)
        result["push"] = _send_daily_push(paths.base_dir, result["health"])
    else:
        result["health"] = {"status": "not_published"}
        result["push"] = {"status": "disabled"}
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Check missing local daily data and run daily update scripts.")
    parser.add_argument("--base-dir", default=str(Path.cwd()))
    parser.add_argument("--asof", default=None)
    parser.add_argument("--days", type=int, default=1)
    parser.add_argument("--target-day", action="append", default=[])
    parser.add_argument("--domains", nargs="+", choices=CORE_UPDATE_DOMAINS, default=list(CORE_UPDATE_DOMAINS))
    parser.add_argument("--skip-mining", action="store_true")
    parser.add_argument("--skip-concept", action="store_true")
    parser.add_argument("--no-health", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--timeout-sec", type=int, default=DEFAULT_CORE_TIMEOUT_SECONDS)
    parser.add_argument("--formal-reserve-sec", type=int, default=DEFAULT_FORMAL_RESERVE_SECONDS)
    parser.add_argument("--use-remote-calendar", action="store_true")
    parser.add_argument("--market-workers", type=int, default=DEFAULT_MARKET_WORKERS)
    parser.add_argument(
        "--market-min-request-interval-sec",
        type=float,
        default=DEFAULT_MARKET_MIN_REQUEST_INTERVAL_SEC,
    )
    args = parser.parse_args()

    result = run_offline_update(
        args.base_dir,
        asof=args.asof,
        days=max(1, int(args.days)),
        target_days=args.target_day,
        include_mining=not bool(args.skip_mining),
        include_concept=not bool(args.skip_concept),
        publish_health=not bool(args.no_health),
        dry_run=bool(args.dry_run),
        timeout_sec=int(args.timeout_sec),
        use_remote_calendar=bool(args.use_remote_calendar),
        market_workers=max(1, int(args.market_workers)),
        market_min_request_interval_sec=max(0.0, float(args.market_min_request_interval_sec)),
        domains=args.domains,
        formal_reserve_seconds=int(args.formal_reserve_sec),
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
    print("scheduler_status=" + json.dumps(result.get("scheduler_status") or {}, ensure_ascii=False, sort_keys=True))
    print(f"ok={bool(result.get('ok'))}")
    return 0 if result.get("ok") or args.dry_run else 1


if __name__ == "__main__":
    raise SystemExit(main())
