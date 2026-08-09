"""Shadow-only formal history bootstrap for isolated screening acceptance."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any

from .capabilities import SCREENING_DEFINITION_VERSION, formal_strategy_ids
from .candidate_persistence import (
    evaluate_formal_capabilities,
    migrate_selection_batch_schema,
    persist_close_final_batch,
)
from .data_quality import usable_stock_trade_dates
from .db import connect
from .selection_context import build_selection_context
from .watchlist import a_history_coverage


SOURCE_DB_NAMES = ("a_share_mvp.db", "etf_mvp.db", "mining_mvp.db", "ths_concept.db")
EVIDENCE_SCHEMA_VERSION = 2


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def _generator_provenance() -> dict[str, str]:
    source_path = Path(__file__).resolve()
    repository = source_path.parents[1]
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    return {
        "git_commit": completed.stdout.strip().lower(),
        "source_path": source_path.relative_to(repository).as_posix(),
        "source_sha256": _sha256(source_path),
    }


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
        for table in (
            "selection_batches",
            "strategy_runs",
            "candidates",
            "outcomes",
            "pullback_state_history",
        )
    }


def _increments(before: dict[str, int], after: dict[str, int]) -> dict[str, dict[str, int]]:
    return {
        table: {"before": before[table], "after": after[table], "delta": after[table] - before[table]}
        for table in before
    }


def _candidate_counts(results: Any) -> dict[str, int]:
    return {result.strategy_id: len(result.candidates) for result in results}


def _complete_batch_record(
    conn: sqlite3.Connection,
    trade_date: str,
) -> tuple[int, str] | None:
    row = conn.execute(
        """
        SELECT batch_id, input_fingerprint
        FROM selection_batches
        WHERE trade_date=? AND definition_version=?
          AND mode='close_final' AND status='complete'
        ORDER BY completed_at DESC, batch_id DESC
        LIMIT 1
        """,
        (str(trade_date), SCREENING_DEFINITION_VERSION),
    ).fetchone()
    if row is None:
        return None
    return int(row[0]), str(row[1])


def _persisted_day_evidence(
    conn: sqlite3.Connection,
    batch_id: int,
) -> tuple[dict[str, int], dict[str, int], list[dict[str, object]]]:
    candidate_counts = {strategy_id: 0 for strategy_id in formal_strategy_ids()}
    for strategy_id, count in conn.execute(
        "SELECT strategy_id, n_candidates FROM strategy_runs WHERE batch_id=? AND mode='close_final'",
        (int(batch_id),),
    ).fetchall():
        if str(strategy_id) in candidate_counts:
            candidate_counts[str(strategy_id)] = int(count or 0)
    c_state_counts = {
        str(state): int(count)
        for state, count in conn.execute(
            """
            SELECT state, COUNT(*)
            FROM pullback_state_history
            WHERE batch_id=? AND definition_version=?
            GROUP BY state ORDER BY state
            """,
            (int(batch_id), SCREENING_DEFINITION_VERSION),
        ).fetchall()
    }
    c_retriggers: list[dict[str, object]] = []
    for sec_code, features_json in conn.execute(
        """
        SELECT c.sec_code, c.features_json
        FROM candidates c
        JOIN strategy_runs r ON r.run_id=c.run_id
        WHERE r.batch_id=? AND r.mode='close_final'
          AND c.strategy_id='second_launch' AND c.version=?
        ORDER BY c.rank, c.sec_code
        """,
        (int(batch_id), SCREENING_DEFINITION_VERSION),
    ).fetchall():
        try:
            features = json.loads(features_json or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            features = {}
        c_retriggers.append(
            {
                "sec_code": str(sec_code).zfill(6),
                "state": features.get("state"),
                "pullback_pct": features.get("pullback_pct"),
                "reclaim_ma10": features.get("reclaim_ma10"),
                "activity_expand": features.get("activity_expand"),
            }
        )
    return candidate_counts, c_state_counts, c_retriggers


def _source_hashes(base: Path) -> dict[str, str]:
    missing = [name for name in SOURCE_DB_NAMES if not (base / name).is_file()]
    if missing:
        raise FileNotFoundError("missing sandbox databases: " + ", ".join(missing))
    return {name: _sha256(base / name) for name in SOURCE_DB_NAMES}


def _manifest(
    base: Path,
    *,
    target_date: str,
    sessions: int,
    source_sha256: dict[str, str],
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "base_dir": str(base),
        "target_date": str(target_date),
        "sessions": int(sessions),
        "definition_version": SCREENING_DEFINITION_VERSION,
        "source_sha256": source_sha256,
    }


def _write_json_atomic(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def run_history_bootstrap_dry_run(
    base_dir: str | Path,
    target_date: str,
    *,
    sessions: int = 60,
    shadow_db_path: str | Path,
    reuse_shadow: bool = False,
) -> dict[str, object]:
    """Replay prior usable sessions into an explicitly provenance-bound shadow database."""
    if int(sessions) <= 0:
        raise ValueError("sessions must be positive")
    base = _isolated_base_dir(base_dir)
    source_hash_before = _source_hashes(base)
    expected_manifest = _manifest(
        base,
        target_date=str(target_date),
        sessions=int(sessions),
        source_sha256=source_hash_before,
    )
    shadow_mining_db = Path(shadow_db_path).resolve()
    if base != shadow_mining_db.parent and base not in shadow_mining_db.parents:
        raise ValueError("shadow database must be inside --base-dir")
    if shadow_mining_db.name in SOURCE_DB_NAMES:
        raise ValueError("shadow database must not replace a source database")
    manifest_path = Path(str(shadow_mining_db) + ".manifest.json")
    if reuse_shadow:
        if not shadow_mining_db.is_file() or not manifest_path.is_file():
            raise FileNotFoundError("--reuse-shadow requires both shadow database and manifest")
        try:
            actual_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError(f"shadow manifest is unreadable: {type(exc).__name__}: {exc}") from exc
        if actual_manifest != expected_manifest:
            raise ValueError("shadow manifest does not match source hashes, target, sessions, or definition version")
        shadow_created = False
    else:
        if shadow_mining_db.exists() or manifest_path.exists():
            raise FileExistsError("shadow or manifest already exists; use a new path or explicit --reuse-shadow")
        shadow_mining_db.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(base / "mining_mvp.db", shadow_mining_db)
        _write_json_atomic(manifest_path, expected_manifest)
        shadow_created = True

    conn = connect(base_dir=base, mining_db_path=shadow_mining_db)
    try:
        migrate_selection_batch_schema(conn)
        usable_dates, _ = usable_stock_trade_dates(conn, end_date=str(target_date), include_end=False)
        replay_dates = list(usable_dates[-int(sessions):])
        coverage_dates = [*usable_dates, str(target_date)]
        counts_before = _table_counts(conn)
        coverage_before = a_history_coverage(conn, str(target_date), usable_dates=coverage_dates)
        strategy_ids = formal_strategy_ids()
        total_candidates = {strategy_id: 0 for strategy_id in strategy_ids}
        empty_by_strategy = {strategy_id: 0 for strategy_id in strategy_ids}
        daily: list[dict[str, object]] = []
        processed = 0
        skipped_insufficient_history = 0
        empty = 0
        reused_batches = 0
        failed = 0
        failures: list[dict[str, str]] = []

        for trade_date in replay_dates:
            try:
                complete_record = _complete_batch_record(conn, str(trade_date)) if reuse_shadow else None
                if complete_record is not None:
                    matching_batch_id, stored_fingerprint = complete_record
                    candidate_counts, c_state_counts, c_retriggers = _persisted_day_evidence(
                        conn,
                        matching_batch_id,
                    )
                    for strategy_id in strategy_ids:
                        count = int(candidate_counts.get(strategy_id, 0))
                        total_candidates[strategy_id] += count
                        empty_by_strategy[strategy_id] += int(count == 0)
                    processed += 1
                    reused_batches += 1
                    empty += int(not any(candidate_counts.values()))
                    daily.append(
                        {
                            "trade_date": str(trade_date),
                            "input_fingerprint": stored_fingerprint,
                            "status": "complete",
                            "batch_id": matching_batch_id,
                            "reused": True,
                            "candidate_counts": candidate_counts,
                            "c_state_counts": c_state_counts,
                            "c_retriggers": c_retriggers,
                            "error": None,
                        }
                    )
                    continue
                context = build_selection_context(conn, str(trade_date), mode="close_final")
                if context.trend_profile is None:
                    skipped_insufficient_history += 1
                    daily.append(
                        {
                            "trade_date": str(trade_date),
                            "input_fingerprint": context.input_fingerprint,
                            "status": "skipped_insufficient_history",
                            "reused": False,
                            "candidate_counts": {strategy_id: 0 for strategy_id in strategy_ids},
                            "c_state_counts": {},
                            "c_retriggers": [],
                        }
                    )
                    continue
                results = evaluate_formal_capabilities(conn, context)
                candidate_counts = _candidate_counts(results)
                for strategy_id in strategy_ids:
                    count = int(candidate_counts.get(strategy_id, 0))
                    total_candidates[strategy_id] += count
                    empty_by_strategy[strategy_id] += int(count == 0)
                c_result = next(result for result in results if result.strategy_id == "second_launch")
                c_state_counts = dict(sorted(Counter(row.state for row in c_result.state_rows).items()))
                c_retriggers = [
                    {
                        "sec_code": row.sec_code,
                        "state": row.state,
                        "pullback_pct": row.pullback_pct,
                        "reclaim_ma10": row.reclaim_ma10,
                        "activity_expand": row.activity_expand,
                    }
                    for row in c_result.state_rows
                    if row.state == "再启动"
                ]
                persisted = persist_close_final_batch(conn, context, results)
                if persisted.status == "complete":
                    processed += 1
                    reused_batches += int(persisted.reused)
                    empty += int(not any(candidate_counts.values()))
                else:
                    failed += 1
                    failures.append(
                        {"trade_date": str(trade_date), "status": persisted.status, "error": persisted.error_msg or ""}
                    )
                daily.append(
                    {
                        "trade_date": str(trade_date),
                        "input_fingerprint": context.input_fingerprint,
                        "status": persisted.status,
                        "batch_id": persisted.batch_id,
                        "reused": persisted.reused,
                        "candidate_counts": candidate_counts,
                        "c_state_counts": c_state_counts,
                        "c_retriggers": c_retriggers,
                        "error": persisted.error_msg,
                    }
                )
            except Exception as exc:
                failed += 1
                error = f"{type(exc).__name__}: {exc}"
                failures.append({"trade_date": str(trade_date), "status": "exception", "error": error})
                daily.append(
                    {
                        "trade_date": str(trade_date),
                        "input_fingerprint": None,
                        "status": "exception",
                        "reused": False,
                        "candidate_counts": {strategy_id: 0 for strategy_id in strategy_ids},
                        "c_state_counts": {},
                        "c_retriggers": [],
                        "error": error,
                    }
                )

        counts_after = _table_counts(conn)
        coverage_after = a_history_coverage(conn, str(target_date), usable_dates=coverage_dates)
    finally:
        conn.close()

    source_hash_after = _source_hashes(base)
    if source_hash_before != source_hash_after:
        failed += 1
        failures.append(
            {"trade_date": "source", "status": "source_hash_changed", "error": "one or more source databases changed"}
        )
    return {
        "evidence_schema_version": EVIDENCE_SCHEMA_VERSION,
        "generator": _generator_provenance(),
        "mode": "history_bootstrap_dry_run",
        "base_dir": str(base),
        "target_date": str(target_date),
        "sessions_requested": int(sessions),
        "replay_dates": replay_dates,
        "shadow_mining_db": str(shadow_mining_db),
        "manifest_path": str(manifest_path),
        "manifest": expected_manifest,
        "shadow_created": shadow_created,
        "source_sha256_before": source_hash_before,
        "source_sha256_after": source_hash_after,
        "processed": processed,
        "skipped_insufficient_history": skipped_insufficient_history,
        "empty": empty,
        "reused_batches": reused_batches,
        "failed": failed,
        "failures": failures,
        "empty_by_strategy": empty_by_strategy,
        "candidate_counts": total_candidates,
        "daily": daily,
        "increments": _increments(counts_before, counts_after),
        "a_history_coverage_before": coverage_before,
        "a_history_coverage_after": coverage_after,
    }


def write_history_bootstrap_report(result: dict[str, object], output_path: str | Path) -> Path:
    """Write the deterministic dry-run record requested by the CLI."""
    path = Path(output_path)
    _write_json_atomic(path, result)
    return path
