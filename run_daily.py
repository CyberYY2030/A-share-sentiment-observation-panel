from __future__ import annotations

import argparse
import datetime as dt
from pathlib import Path
from typing import Any

from mining.backtest import backfill_outcomes, backfill_snapshot_outcomes
from mining.capabilities import formal_definitions, formal_strategy_ids
from mining.candidate_persistence import (
    evaluate_formal_capabilities,
    persist_close_final_batch,
    selection_batch_schema_ready,
)
from mining.db import (
    connect,
    create_strategy_run,
    latest_stock_trade_date,
    list_stock_trade_dates,
    register_strategy,
    save_candidates,
)
from mining.intraday import scan_intraday
from mining.refresh_basics import refresh_basics
from mining.reports import generate_excel_report, generate_markdown_report
from mining.scanners import get_registered_scanners
from mining.selection_context import build_selection_context
from mining.scanners.momentum_breakout import (
    MomentumBreakoutScanner,
    select_candidates_from_universe as select_momentum_candidates,
)
from mining.scanners.rps_stock import (
    RpsStockScanner,
    select_candidates_from_universe as select_rps_candidates,
)
from mining.universe import build_universe
from mining.watchlist import persist_watchlist_snapshot


def _run_legacy_scanners(
    conn: Any,
    trade_date: str,
    *,
    include_formal: bool = False,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    formal_ids = set(formal_strategy_ids())
    scanners = [
        scanner_cls()
        for scanner_cls in get_registered_scanners()
        if include_formal or scanner_cls.strategy_id not in formal_ids
    ]
    stock_universe = None
    rps_stock_candidates = None
    rps_exclusion_codes: set[str] | None = None

    for scanner in scanners:
        register_strategy(conn, scanner)
        if scanner.strategy_id == MomentumBreakoutScanner.strategy_id:
            if stock_universe is None:
                stock_universe = build_universe(conn, trade_date)
            scanner.last_universe_size = len(stock_universe)
            if rps_stock_candidates is None:
                rps_scanner = RpsStockScanner()
                rps_stock_candidates = select_rps_candidates(
                    conn=conn,
                    trade_date=trade_date,
                    universe=stock_universe,
                    params=rps_scanner.params,
                    strategy_id=rps_scanner.strategy_id,
                    version=rps_scanner.version,
                )
                rps_exclusion_codes = {candidate.sec_code for candidate in rps_stock_candidates}
            candidates = select_momentum_candidates(
                conn=conn,
                trade_date=trade_date,
                universe=stock_universe,
                params=scanner.params,
                strategy_id=scanner.strategy_id,
                version=scanner.version,
                rps_exclusion_codes=rps_exclusion_codes,
            )
        elif scanner.strategy_id == RpsStockScanner.strategy_id:
            if stock_universe is None:
                stock_universe = build_universe(conn, trade_date)
            scanner.last_universe_size = len(stock_universe)
            if rps_stock_candidates is None:
                rps_stock_candidates = select_rps_candidates(
                    conn=conn,
                    trade_date=trade_date,
                    universe=stock_universe,
                    params=scanner.params,
                    strategy_id=scanner.strategy_id,
                    version=scanner.version,
                )
            candidates = rps_stock_candidates
        else:
            candidates = scanner.run(conn, trade_date)
        status = "ok" if candidates else "empty"
        run_id = create_strategy_run(
            conn,
            scanner,
            trade_date=trade_date,
            status=status,
            n_candidates=len(candidates),
        )
        save_candidates(conn, run_id, candidates)
        results.append(
            {
                "strategy_id": scanner.strategy_id,
                "status": status,
                "count": len(candidates),
            }
        )
    return results


# Compatibility seam for legacy callers and tests.  The daily production path below
# invokes _run_legacy_scanners after the formal batch path has completed.
def _run_scanners(conn: Any, trade_date: str) -> list[dict[str, Any]]:
    return _run_legacy_scanners(conn, trade_date, include_formal=True)


def _run_formal_capabilities(conn: Any, trade_date: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Evaluate A-E once from a shared close-final context and persist one batch."""
    if not selection_batch_schema_ready(conn):
        return (
            [
                {"strategy_id": definition.strategy_id, "status": "migration_required", "count": 0}
                for definition in formal_definitions()
            ],
            {"status": "migration_required", "batch_id": None},
        )
    context = build_selection_context(conn, trade_date, mode="close_final")
    capability_results = evaluate_formal_capabilities(conn, context)
    persisted = persist_close_final_batch(conn, context, capability_results)
    summary = [
        {
            "strategy_id": result.strategy_id,
            "status": result.status,
            "count": len(result.candidates),
        }
        for result in capability_results
    ]
    return summary, {
        "status": persisted.status,
        "batch_id": persisted.batch_id,
        "reused": persisted.reused,
        "error": persisted.error_msg,
    }


def execute_daily_pipeline(
    base_dir: str | Path = ".",
    trade_date: str | None = None,
    refresh: bool = True,
    out_dir: str | Path | None = None,
    emit_reports: bool = True,
) -> dict[str, Any]:
    conn = connect(base_dir=base_dir)
    try:
        resolved_trade_date = trade_date or latest_stock_trade_date(conn)
        if resolved_trade_date is None:
            raise RuntimeError("No stock trade date available in the source database.")

        refresh_result = None
        if refresh:
            refresh_result = refresh_basics(conn, trade_date=resolved_trade_date)
        formal_results, formal_batch = _run_formal_capabilities(conn, resolved_trade_date)
        if formal_batch["status"] == "migration_required":
            scanner_results = _run_scanners(conn, resolved_trade_date)
        else:
            legacy_results = _run_legacy_scanners(conn, resolved_trade_date)
            scanner_results = [*formal_results, *legacy_results]
        backfill_result = backfill_outcomes(conn, trade_date=resolved_trade_date)
        watchlist_validation = None
        try:
            watchlist_rows = persist_watchlist_snapshot(conn, resolved_trade_date)
            snapshot_backfill = backfill_snapshot_outcomes(conn)
            watchlist_validation = {
                "snapshots": watchlist_rows,
                "backfill": snapshot_backfill,
            }
        except Exception as exc:
            watchlist_validation = {"error": str(exc)}

        markdown_path = None
        excel_path = None
        if emit_reports:
            target_dir = out_dir or Path(base_dir) / "output"
            markdown_path = generate_markdown_report(conn, resolved_trade_date, target_dir)
            excel_path = generate_excel_report(conn, resolved_trade_date, target_dir)

        return {
            "trade_date": resolved_trade_date,
            "refresh": refresh_result,
            "scanners": scanner_results,
            "formal_batch": formal_batch,
            "backfill": backfill_result,
            "watchlist_validation": watchlist_validation,
            "markdown": markdown_path,
            "excel": excel_path,
        }
    finally:
        conn.close()


def execute_range_pipeline(
    base_dir: str | Path,
    start_date: str,
    end_date: str,
    out_dir: str | Path | None = None,
) -> dict[str, Any]:
    conn = connect(base_dir=base_dir)
    try:
        trade_dates = [
            day for day in list_stock_trade_dates(conn) if start_date <= day <= end_date
        ]
    finally:
        conn.close()

    last_result = None
    for trade_date in trade_dates:
        last_result = execute_daily_pipeline(
            base_dir=base_dir,
            trade_date=trade_date,
            refresh=False,
            out_dir=out_dir,
            emit_reports=False,
        )
    if last_result is None:
        raise RuntimeError("No trade dates found in the requested range.")
    return execute_daily_pipeline(
        base_dir=base_dir,
        trade_date=end_date,
        refresh=False,
        out_dir=out_dir,
        emit_reports=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the mining daily pipeline.")
    parser.add_argument("--date", dest="trade_date")
    parser.add_argument("--base-dir", default=Path.cwd())
    parser.add_argument("--backfill-only", action="store_true")
    parser.add_argument("--range", nargs=2, metavar=("START", "END"))
    parser.add_argument("--intraday", action="store_true")
    parser.add_argument("--intraday-start", default="14:00")
    parser.add_argument("--intraday-end", default="15:00")
    args = parser.parse_args()

    if args.intraday:
        conn = connect(base_dir=args.base_dir)
        try:
            result = scan_intraday(
                conn,
                out_dir=Path(args.base_dir) / "output" / "intraday",
                window_start=dt.time.fromisoformat(args.intraday_start),
                window_end=dt.time.fromisoformat(args.intraday_end),
            )
        finally:
            conn.close()
        print(result["display"].to_string(index=False) if not result["display"].empty else "no intraday launch candidates")
        print(f"report={result['report']}")
        return
    if args.range:
        result = execute_range_pipeline(
            base_dir=args.base_dir,
            start_date=args.range[0],
            end_date=args.range[1],
        )
    elif args.backfill_only:
        conn = connect(base_dir=args.base_dir)
        try:
            result = {
                "trade_date": args.trade_date,
                "backfill": backfill_outcomes(conn, trade_date=args.trade_date),
            }
        finally:
            conn.close()
    else:
        result = execute_daily_pipeline(
            base_dir=args.base_dir,
            trade_date=args.trade_date,
        )

    print(result)


if __name__ == "__main__":
    main()
