"""Shadow-only formal history bootstrap for isolated screening acceptance."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from pathlib import Path
from typing import Any

from .candidate_persistence import (
    evaluate_formal_capabilities,
    migrate_selection_batch_schema,
    persist_close_final_batch,
)
from .data_quality import usable_stock_trade_dates
from .db import connect
from .selection_context import build_selection_context
from .watchlist import a_history_coverage


SHADOW_MINING_DB_NAME = "r4_1-history-shadow-mining_mvp.db"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def _isolated_base_dir(base_dir: str | Path) -> Path:
    base = Path(base_dir).resolve()
    configured = os.environ.get("SCREENING_BASE_DIR", "").strip()
    if not configured:
        raise RuntimeError("history bootstrap dry run requires SCREENING_BASE_DIR")
    if Path(configured).resolve() != base:
        raise RuntimeError("SCREENING_BASE_DIR must resolve to --base-dir for history bootstrap")
    return base


def _table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in ("selection_batches", "strategy_runs", "candidates", "outcomes")
    }


def _increments(before: dict[str, int], after: dict[str, int]) -> dict[str, dict[str, int]]:
    return {
        table: {"before": before[table], "after": after[table], "delta": after[table] - before[table]}
        for table in before
    }


def _candidate_counts(results: Any) -> dict[str, int]:
    return {result.strategy_id: len(result.candidates) for result in results}


def run_history_bootstrap_dry_run(
    base_dir: str | Path,
    target_date: str,
    *,
    sessions: int = 60,
) -> dict[str, object]:
    """Replay prior usable sessions into a reusable shadow mining database only."""
    if int(sessions) <= 0:
        raise ValueError("sessions must be positive")
    base = _isolated_base_dir(base_dir)
    source_mining_db = base / "mining_mvp.db"
    if not source_mining_db.is_file():
        raise FileNotFoundError(f"missing sandbox mining database: {source_mining_db}")
    source_hash_before = _sha256(source_mining_db)
    shadow_mining_db = base / SHADOW_MINING_DB_NAME
    shadow_created = not shadow_mining_db.exists()
    if shadow_created:
        shutil.copy2(source_mining_db, shadow_mining_db)

    conn = connect(base_dir=base, mining_db_path=shadow_mining_db)
    try:
        migrate_selection_batch_schema(conn)
        usable_dates, _ = usable_stock_trade_dates(conn, end_date=str(target_date), include_end=False)
        replay_dates = list(usable_dates[-int(sessions):])
        coverage_dates = [*usable_dates, str(target_date)]
        counts_before = _table_counts(conn)
        coverage_before = a_history_coverage(conn, str(target_date), usable_dates=coverage_dates)
        total_candidates: dict[str, int] = {}
        fingerprints: list[dict[str, object]] = []
        processed = 0
        skipped_insufficient_history = 0
        empty = 0
        reused_batches = 0

        for trade_date in replay_dates:
            context = build_selection_context(conn, str(trade_date), mode="close_final")
            if context.trend_profile is None:
                skipped_insufficient_history += 1
                fingerprints.append(
                    {
                        "trade_date": str(trade_date),
                        "input_fingerprint": context.input_fingerprint,
                        "status": "skipped_insufficient_history",
                        "reused": False,
                    }
                )
                continue
            results = evaluate_formal_capabilities(conn, context)
            candidate_counts = _candidate_counts(results)
            for strategy_id, count in candidate_counts.items():
                total_candidates[strategy_id] = total_candidates.get(strategy_id, 0) + int(count)
            persisted = persist_close_final_batch(conn, context, results)
            if persisted.status == "complete":
                processed += 1
                reused_batches += int(persisted.reused)
                empty += int(not any(candidate_counts.values()))
            fingerprints.append(
                {
                    "trade_date": str(trade_date),
                    "input_fingerprint": context.input_fingerprint,
                    "status": persisted.status,
                    "batch_id": persisted.batch_id,
                    "reused": persisted.reused,
                    "candidate_counts": candidate_counts,
                    "error": persisted.error_msg,
                }
            )

        counts_after = _table_counts(conn)
        coverage_after = a_history_coverage(conn, str(target_date), usable_dates=coverage_dates)
    finally:
        conn.close()

    source_hash_after = _sha256(source_mining_db)
    if source_hash_before != source_hash_after:
        raise RuntimeError("sandbox mining source changed during history bootstrap dry run")
    return {
        "mode": "history_bootstrap_dry_run",
        "base_dir": str(base),
        "target_date": str(target_date),
        "sessions_requested": int(sessions),
        "replay_dates": replay_dates,
        "shadow_mining_db": str(shadow_mining_db),
        "shadow_created": shadow_created,
        "source_mining_sha256_before": source_hash_before,
        "source_mining_sha256_after": source_hash_after,
        "processed": processed,
        "skipped_insufficient_history": skipped_insufficient_history,
        "empty": empty,
        "reused_batches": reused_batches,
        "candidate_counts": total_candidates,
        "fingerprints": fingerprints,
        "increments": _increments(counts_before, counts_after),
        "a_history_coverage_before": coverage_before,
        "a_history_coverage_after": coverage_after,
    }


def write_history_bootstrap_report(result: dict[str, object], output_path: str | Path) -> Path:
    """Write the deterministic dry-run record requested by the CLI."""
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path
