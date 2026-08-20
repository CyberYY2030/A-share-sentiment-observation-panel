from __future__ import annotations

import math
import json
import sqlite3
import datetime as dt
from dataclasses import replace
from pathlib import Path
from typing import Callable

import pandas as pd
import streamlit as st

from ..db import connect, list_stock_trade_dates
from ..capabilities import (
    LEGACY_STRATEGY_IDS,
    SCREENING_DEFINITION_VERSION,
    formal_definitions,
    shortlist_capability_rows,
    visible_strategy_ids,
)
from ..data_quality import (
    STATUS_CLEAN,
    STATUS_USABLE_WITH_QUARANTINE,
    inspect_stock_session,
    usable_stock_trade_dates,
)
from ..features import (
    board_kind,
    change_pct,
    high_breakout_from_close_pct,
    high_breakout_from_open_pct,
    upper_shadow_pct,
)
from ..scanners import load_builtin_scanners
from ..scanners.momentum_breakout import (
    MomentumBreakoutScanner,
    select_candidates_from_universe as select_momentum_candidates,
)
from ..scanners.rps_stock import (
    RpsStockScanner,
    select_candidates_from_universe as select_rps_candidates,
)
from ..universe import build_universe
from ..reports import compute_market_regime
from ..quote_snapshot import QuoteSnapshotAdapter, QuoteSnapshotResult
from ..selection_batches import latest_complete_batch, selection_batch_schema_state
from ..selection_runtime import (
    CHINA_TZ,
    MODE_CLOSE_FINAL,
    MODE_CLOSE_PENDING,
    MODE_INTRADAY,
    SelectionRuntime,
)
from ..scanners.base_breakout import evaluate_base_breakout
from ..scanners.counter_trend_rs import evaluate_counter_trend_rs
from ..scanners.launch_burst import evaluate_compression_launch
from ..scanners.momentum_breakout import evaluate_momentum_anomaly
from ..scanners.strong_trend import evaluate_strong_trend
from ..scanners.second_launch import select_candidates_from_pullback_support
from ..watchlist import (
    STATE_EXTEND,
    STATE_READY,
    STATE_RETRIGGER,
    a_history_coverage,
    build_watchlist,
    build_pullback_support,
    split_actionable_watchlist,
)
from ..playbook import lookup_playbook
from run_daily import execute_daily_pipeline
from runtime_paths import build_runtime_paths


SnapshotLoader = Callable[[], QuoteSnapshotResult | pd.DataFrame]


def _safe_float(value: object) -> float:
    try:
        if value is None:
            return math.nan
        return float(value)
    except (TypeError, ValueError):
        return math.nan


def _canonical_code(value: object) -> str:
    text = "".join(ch for ch in str(value or "") if ch.isdigit())
    return text[-6:].zfill(6) if text else ""


def _pick_col(df: pd.DataFrame, candidates: list[str]) -> str | None:
    columns = list(df.columns)
    lowered = {str(col).strip().lower(): str(col) for col in columns}
    for candidate in candidates:
        if candidate in columns:
            return candidate
        match = lowered.get(str(candidate).strip().lower())
        if match:
            return match
    return None


def _scale_amount_to_yuan(series: pd.Series) -> pd.Series:
    scaled = pd.to_numeric(series, errors="coerce")
    median = float(scaled.dropna().median()) if scaled.dropna().size else math.nan
    if math.isfinite(median) and median < 1e8:
        return scaled * 1e4
    return scaled


def _normalize_snapshot_quotes(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame(
            columns=[
                "sec_code",
                "close",
                "pre_close",
                "open",
                "high",
                "low",
                "volume",
                "amount",
                "change_pct",
            ]
        )

    code_col = _pick_col(df, ["code", "symbol", "sec_code", "代码", "证券代码", "股票代码"])
    close_col = _pick_col(df, ["close", "last", "price", "最新价", "现价", "收盘"])
    pre_close_col = _pick_col(df, ["pre_close", "previous_close", "昨收", "昨收价"])
    open_col = _pick_col(df, ["open", "今开", "开盘"])
    high_col = _pick_col(df, ["high", "最高"])
    low_col = _pick_col(df, ["low", "最低"])
    volume_col = _pick_col(df, ["volume", "vol", "成交量", "成交量(手)"])
    amount_col = _pick_col(df, ["amount", "turnover", "成交额", "成交金额"])
    pct_col = _pick_col(df, ["change_pct", "pct_chg", "pct", "changepercent", "涨跌幅"])

    name_col = _pick_col(df, ["name", "sec_name"])

    if code_col is None or close_col is None:
        return pd.DataFrame(
            columns=[
                "sec_code",
                "close",
                "pre_close",
                "open",
                "high",
                "low",
                "volume",
                "amount",
                "change_pct",
            ]
        )

    out = pd.DataFrame()
    out["sec_code"] = df[code_col].map(_canonical_code)
    out["sec_name"] = df[name_col].astype(str).str.strip() if name_col else pd.NA
    out["close"] = df[close_col].map(_safe_float)
    out["pre_close"] = df[pre_close_col].map(_safe_float) if pre_close_col else math.nan
    out["open"] = df[open_col].map(_safe_float) if open_col else math.nan
    out["high"] = df[high_col].map(_safe_float) if high_col else math.nan
    out["low"] = df[low_col].map(_safe_float) if low_col else math.nan
    out["volume"] = pd.to_numeric(df[volume_col], errors="coerce") if volume_col else math.nan
    out["amount"] = _scale_amount_to_yuan(df[amount_col]) if amount_col else math.nan
    out["change_pct"] = df[pct_col].map(_safe_float) if pct_col else math.nan

    need_pct = (
        pd.to_numeric(out["change_pct"], errors="coerce").isna()
        & pd.to_numeric(out["pre_close"], errors="coerce").gt(0)
        & pd.to_numeric(out["close"], errors="coerce").notna()
    )
    out.loc[need_pct, "change_pct"] = (
        (out.loc[need_pct, "close"] / out.loc[need_pct, "pre_close"]) - 1.0
    ) * 100.0

    out = out[out["sec_code"].str.match(r"^(0|3|6)\d{5}$", na=False)].copy()
    out = out.dropna(subset=["sec_code", "close"])
    return out.reset_index(drop=True)


def _build_cached_snapshot_loader(snapshot_loader: SnapshotLoader | None = None) -> SnapshotLoader:
    cached: QuoteSnapshotResult | pd.DataFrame | None = None

    def _copy(value: QuoteSnapshotResult | pd.DataFrame) -> QuoteSnapshotResult | pd.DataFrame:
        if isinstance(value, QuoteSnapshotResult):
            return replace(value, frame=value.frame.copy())
        return value.copy()

    def _loader() -> QuoteSnapshotResult | pd.DataFrame:
        nonlocal cached
        if cached is None:
            cached = snapshot_loader() if snapshot_loader is not None else pd.DataFrame()
        return _copy(cached)

    return _loader


def _expected_snapshot_codes(base_dir: str | Path, trade_date: str) -> set[str]:
    runtime_paths = build_runtime_paths(str(Path(base_dir).resolve()))
    stock_db = Path(runtime_paths.stock_db).resolve()
    conn = sqlite3.connect(f"{stock_db.as_uri()}?mode=ro&immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        usable_dates, _ = usable_stock_trade_dates(
            conn,
            end_date=str(trade_date),
            include_end=False,
        )
        if not usable_dates:
            return set()
        reference_date = usable_dates[-1]
        rows = conn.execute(
            "SELECT DISTINCT sec_code FROM kline_daily WHERE sec_type='stock' AND trade_date=?",
            (str(reference_date),),
        ).fetchall()
        return {
            str(row[0]).zfill(6)
            for row in rows
            if str(row[0]).zfill(6).startswith(("0", "3", "6"))
        }
    finally:
        conn.close()


def _build_quote_snapshot_loader(
    base_dir: str | Path,
    trade_date: str,
    *,
    adapter: QuoteSnapshotAdapter | None = None,
    now: dt.datetime | None = None,
    cache_only: bool = False,
) -> SnapshotLoader:
    expected_codes = _expected_snapshot_codes(base_dir, trade_date)
    resolved_adapter = adapter or QuoteSnapshotAdapter(
        cache_dir=Path(base_dir) / "output" / "screening-v2-work" / "cache",
        minimum_expected_codes=5_000,
    )
    current = now or dt.datetime.now(dt.timezone(dt.timedelta(hours=8)))

    def _loader() -> QuoteSnapshotResult:
        return resolved_adapter.load(
            str(trade_date),
            expected_codes=expected_codes,
            now=current,
            cache_only=cache_only,
        )

    return _build_cached_snapshot_loader(_loader)


def _quote_result(value: QuoteSnapshotResult | pd.DataFrame) -> QuoteSnapshotResult:
    if isinstance(value, QuoteSnapshotResult):
        return value
    frame = _normalize_snapshot_quotes(pd.DataFrame(value))
    status = "snapshot_usable" if not frame.empty else "empty_payload"
    return QuoteSnapshotResult(
        frame=frame,
        provider="injected_loader" if not frame.empty else None,
        observed_at=None,
        raw_rows=len(value),
        normalized_rows=len(frame),
        coverage=0.0,
        attempts=1,
        errors=(),
        status=status,
        from_cache=False,
    )


def _snapshot_frame(value: QuoteSnapshotResult | pd.DataFrame) -> pd.DataFrame:
    return _quote_result(value).frame.copy()


def _snapshot_universe(conn, trade_date: str, latest_quotes: pd.DataFrame) -> pd.DataFrame:
    previous_dates = list_stock_trade_dates(conn, end_date=trade_date, limit=1, include_end=False)
    reference_date = previous_dates[-1] if previous_dates else _resolve_close_trade_date(conn, trade_date)
    if reference_date is None:
        return pd.DataFrame()

    reference_universe = build_universe(conn, reference_date)
    if reference_universe.empty:
        return pd.DataFrame()

    keep = reference_universe[["sec_code", "sec_name"]].drop_duplicates()
    universe = keep.merge(latest_quotes, on="sec_code", how="inner", suffixes=("_metadata", "_snapshot"))
    if universe.empty:
        return universe

    snapshot_name = universe.get("sec_name_snapshot", pd.Series(pd.NA, index=universe.index))
    metadata_name = universe.get("sec_name_metadata", pd.Series(pd.NA, index=universe.index))
    universe["sec_name"] = snapshot_name.fillna(metadata_name).fillna(universe["sec_code"])
    if "change_pct" not in universe.columns:
        universe["change_pct"] = math.nan
    universe["change_pct"] = universe["change_pct"].fillna(universe.apply(change_pct, axis=1))
    universe["board"] = universe["sec_code"].map(board_kind)
    universe = universe[
        (pd.to_numeric(universe["amount"], errors="coerce") > 0)
        & (pd.to_numeric(universe["volume"], errors="coerce") > 0)
    ].copy()
    universe = universe[
        ~(
            (pd.to_numeric(universe["open"], errors="coerce") == pd.to_numeric(universe["high"], errors="coerce"))
            & (pd.to_numeric(universe["high"], errors="coerce") == pd.to_numeric(universe["low"], errors="coerce"))
            & (pd.to_numeric(universe["low"], errors="coerce") == pd.to_numeric(universe["close"], errors="coerce"))
        )
    ].copy()
    return universe.reset_index(drop=True)


def _serialize_candidates(candidates: list) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for candidate in candidates:
        row = {
            "strategy_id": candidate.strategy_id,
            "trade_date": candidate.trade_date,
            "sec_type": candidate.sec_type,
            "sec_code": candidate.sec_code,
            "sec_name": candidate.sec_name,
            "entry_price": candidate.entry_price,
            "rank": candidate.rank,
        }
        row.update(candidate.features or {})
        rows.append(row)
    return pd.DataFrame(rows)


def _run_display_scanners(conn, trade_date: str, universe: pd.DataFrame) -> pd.DataFrame:
    load_builtin_scanners()
    if universe.empty:
        return pd.DataFrame()

    momentum = MomentumBreakoutScanner()
    rps_stock = RpsStockScanner()
    rps_candidates = select_rps_candidates(
        conn=conn,
        trade_date=trade_date,
        universe=universe,
        params=rps_stock.params,
        strategy_id=rps_stock.strategy_id,
        version=rps_stock.version,
    )
    rps_exclusion_codes = {candidate.sec_code for candidate in rps_candidates}
    candidates = [
        *select_momentum_candidates(
            conn=conn,
            trade_date=trade_date,
            universe=universe,
            params=momentum.params,
            strategy_id=momentum.strategy_id,
            version=momentum.version,
            rps_exclusion_codes=rps_exclusion_codes,
        ),
        *rps_candidates,
    ]
    df = _serialize_candidates(candidates)
    if df.empty:
        return df
    return df.sort_values(["strategy_id", "rank", "sec_code"]).reset_index(drop=True)


def _prefilter_momentum_universe(universe: pd.DataFrame, params: dict[str, float]) -> pd.DataFrame:
    if universe.empty:
        return universe

    df = universe.copy()
    if "board" not in df.columns:
        df["board"] = df["sec_code"].map(board_kind)
    df["amount"] = pd.to_numeric(df.get("amount"), errors="coerce")
    current_change_pct = pd.to_numeric(df.get("change_pct"), errors="coerce")
    df["upper_shadow"] = df.apply(upper_shadow_pct, axis=1)
    df["high_over_open_pct"] = df.apply(high_breakout_from_open_pct, axis=1)
    df["high_over_close_pct"] = df.apply(high_breakout_from_close_pct, axis=1)

    main_mask = df["board"] == "main"
    fast_mask = df["board"].isin(["gem", "star"])
    gain_candidate = (
        main_mask
        & (current_change_pct > params["gain_main_lo"])
        & (current_change_pct < params["gain_main_hi"])
    ) | (
        fast_mask
        & (current_change_pct > params["gain_gem_lo"])
        & (current_change_pct < params["gain_gem_hi"])
    )
    shadow_candidate = (
        main_mask
        & (df["high_over_open_pct"] > params["high_over_open_min"])
        & (df["high_over_close_pct"] > params["high_over_close_min"])
        & (current_change_pct > params["shadow_latest_change_pct_floor"])
        & (current_change_pct < params["gain_main_hi"])
    ) | (
        fast_mask
        & (df["high_over_open_pct"] > params["high_over_open_min"])
        & (df["high_over_close_pct"] > params["high_over_close_min"])
        & (current_change_pct > params["shadow_latest_change_pct_floor"])
        & (current_change_pct < params["gain_gem_hi"])
    )
    early_strength_candidate = (
        (df["upper_shadow"] > params["early_strength_upper_shadow_min"])
        & (current_change_pct < params["early_strength_latest_gain_max"])
        & (df["amount"] > params["early_strength_amount_min"])
    )
    amount_candidate = df["amount"] >= params["amount_min"]
    candidate_mask = ((gain_candidate | shadow_candidate) & amount_candidate) | early_strength_candidate
    return df[candidate_mask].reset_index(drop=True)


def _run_display_scanners_for_snapshot(conn, trade_date: str, universe: pd.DataFrame) -> pd.DataFrame:
    load_builtin_scanners()
    if universe.empty:
        return pd.DataFrame()

    momentum = MomentumBreakoutScanner()
    rps_stock = RpsStockScanner()
    rps_candidates = select_rps_candidates(
        conn=conn,
        trade_date=trade_date,
        universe=universe,
        params=rps_stock.params,
        strategy_id=rps_stock.strategy_id,
        version=rps_stock.version,
    )
    rps_exclusion_codes = {candidate.sec_code for candidate in rps_candidates}
    momentum_universe = _prefilter_momentum_universe(universe, momentum.params)
    candidates = [
        *select_momentum_candidates(
            conn=conn,
            trade_date=trade_date,
            universe=momentum_universe,
            params=momentum.params,
            strategy_id=momentum.strategy_id,
            version=momentum.version,
            rps_exclusion_codes=rps_exclusion_codes,
        ),
        *rps_candidates,
    ]
    df = _serialize_candidates(candidates)
    if df.empty:
        return df
    return df.sort_values(["strategy_id", "rank", "sec_code"]).reset_index(drop=True)


def _load_close_opportunities(conn, trade_date: str) -> pd.DataFrame:
    """Historical close mode reads persisted evidence only; it never finalizes on read."""
    return _load_persisted_candidates(conn, trade_date)


def _persisted_candidate_dates(conn) -> list[str]:
    if not selection_batch_schema_state(conn).ready:
        return []
    try:
        rows = conn.execute(
            """
            SELECT DISTINCT trade_date
            FROM selection_batches
            WHERE definition_version=? AND mode='close_final' AND status='complete'
            ORDER BY trade_date
            """,
            (SCREENING_DEFINITION_VERSION,),
        ).fetchall()
    except sqlite3.DatabaseError:
        return []
    return [str(row[0]) for row in rows]


def _legacy_persisted_candidate_dates(conn) -> list[str]:
    """Legacy-only date inventory used by the bounded catch-up path before migration."""
    marks = ",".join("?" for _ in LEGACY_STRATEGY_IDS)
    try:
        rows = conn.execute(
            f"""
            SELECT DISTINCT trade_date FROM candidates
            WHERE sec_type='stock' AND strategy_id IN ({marks})
            ORDER BY trade_date
            """,
            LEGACY_STRATEGY_IDS,
        ).fetchall()
    except sqlite3.DatabaseError:
        return []
    return [str(row[0]) for row in rows]


def _load_formal_capability_candidates(conn, trade_date: str) -> pd.DataFrame:
    """Read one complete current-version batch; never combine old versions or batches."""
    definitions = formal_definitions()
    marks = ",".join("?" for _ in definitions)
    batch = latest_complete_batch(conn, trade_date)
    if batch.code != "complete":
        empty = pd.DataFrame()
        empty.attrs["formal_batch_status"] = batch.code
        empty.attrs["formal_batch_error"] = batch.error_msg
        return empty
    try:
        df = pd.read_sql_query(
            f"""
            SELECT c.strategy_id, c.version, c.trade_date, c.sec_type, c.sec_code,
                   c.sec_name, c.entry_price, c.rank, c.features_json
            FROM candidates c
            JOIN strategy_runs r ON r.run_id=c.run_id
            WHERE r.batch_id=? AND r.mode='close_final'
              AND c.sec_type='stock' AND c.version=?
              AND c.strategy_id IN ({marks}) AND c.trade_date=?
            ORDER BY c.strategy_id, c.rank, c.sec_code
            """,
            conn,
            params=[batch.batch_id, SCREENING_DEFINITION_VERSION, *(definition.strategy_id for definition in definitions), str(trade_date)],
        )
    except (sqlite3.DatabaseError, pd.errors.DatabaseError) as exc:
        empty = pd.DataFrame()
        empty.attrs["formal_batch_status"] = "schema_error"
        empty.attrs["formal_batch_error"] = f"formal candidate query: {type(exc).__name__}: {exc}"
        return empty
    df.attrs["formal_batch_status"] = "complete"
    if df.empty:
        return df
    features = pd.json_normalize(df["features_json"].map(lambda text: json.loads(text or "{}")))
    return _annotate_formal_capability_candidates(
        pd.concat([df.drop(columns=["features_json"]), features], axis=1)
    )


def _load_persisted_candidates(conn, trade_date: str) -> pd.DataFrame:
    """Legacy display rows plus the single formal batch selected above."""
    formal_rows = _load_formal_capability_candidates(conn, trade_date)
    visible_ids = visible_strategy_ids()
    visible_marks = ",".join("?" for _ in visible_ids)
    formal_ids = tuple(definition.strategy_id for definition in formal_definitions())
    formal_marks = ",".join("?" for _ in formal_ids)
    legacy_rows = pd.read_sql_query(
        f"""
        SELECT strategy_id, version, trade_date, sec_type, sec_code, sec_name, entry_price, rank, features_json
        FROM candidates
        WHERE sec_type='stock' AND strategy_id IN ({visible_marks}) AND trade_date=?
          AND strategy_id NOT IN ({formal_marks})
        ORDER BY strategy_id, rank, sec_code
        """,
        conn,
        params=[*visible_ids, str(trade_date), *formal_ids],
    )
    if not legacy_rows.empty:
        features = pd.json_normalize(legacy_rows["features_json"].map(lambda text: json.loads(text or "{}")))
        legacy_rows = pd.concat([legacy_rows.drop(columns=["features_json"]), features], axis=1)
    return pd.concat([formal_rows, legacy_rows], ignore_index=True, sort=False)


def _annotate_formal_capability_candidates(rows: pd.DataFrame) -> pd.DataFrame:
    if rows.empty:
        return rows
    definitions = {definition.strategy_id: definition for definition in formal_definitions()}
    result = rows[rows["strategy_id"].isin(definitions)].copy()
    if result.empty:
        return result
    result["capability"] = result["strategy_id"].map(lambda value: definitions[str(value)].capability)
    result["capability_label"] = result["strategy_id"].map(lambda value: definitions[str(value)].label)
    result["subtype"] = result["strategy_id"].map(lambda value: definitions[str(value)].subtype)
    result["reference_price"] = pd.to_numeric(result.get("reference_price", result["entry_price"]), errors="coerce").fillna(result["entry_price"])
    return result


def _capability_run_status(conn, trade_date: str) -> pd.DataFrame:
    """Return one explicit availability state per formal capability strategy."""
    batch = latest_complete_batch(conn, trade_date)
    base = pd.DataFrame()
    if batch.code == "complete":
        try:
            base = pd.read_sql_query(
                """
                SELECT strategy_id, status, n_candidates, universe_size, run_at
                FROM strategy_runs WHERE batch_id=? AND mode='close_final'
                """,
                conn,
                params=[batch.batch_id],
            )
        except (sqlite3.DatabaseError, pd.errors.DatabaseError) as exc:
            batch = type(batch)(
                "schema_error",
                error_msg=f"formal run query: {type(exc).__name__}: {exc}",
            )
    rows: list[dict[str, object]] = []
    for definition in formal_definitions():
        if batch.code != "complete":
            rows.append(
                {
                    "strategy_id": definition.strategy_id,
                    "capability": definition.capability,
                    "availability": batch.code,
                    "error_msg": batch.error_msg,
                    "last_nonempty": None,
                }
            )
            continue
        matched = base[base["strategy_id"].eq(definition.strategy_id)] if not base.empty else pd.DataFrame()
        if matched.empty:
            rows.append(
                {
                    "strategy_id": definition.strategy_id,
                    "capability": definition.capability,
                    "availability": "unfinalized",
                    "error_msg": "complete batch is missing a strategy run",
                    "last_nonempty": None,
                }
            )
            continue
        item = matched.iloc[0]
        status = str(item["status"])
        availability = "empty" if status == "empty" else ("failed" if status not in {"ok", "empty"} else "complete")
        rows.append({
            "strategy_id": definition.strategy_id,
            "capability": definition.capability,
            "availability": availability,
            "n_candidates": int(item["n_candidates"] or 0),
            "run_at": item["run_at"],
            "last_nonempty": None,
        })
    return pd.DataFrame(rows)

def _filter_pullback_strength_phase(rows: pd.DataFrame, strength_tier: str | None, state: str | None) -> pd.DataFrame:
    result = rows.copy()
    if strength_tier and strength_tier != "全部" and "strength_tier" in result.columns:
        result = result[result["strength_tier"].eq(strength_tier)]
    if state and state != "全部" and "state" in result.columns:
        result = result[result["state"].eq(state)]
    return result.reset_index(drop=True)


def _selection_evidence(
    context,
    *,
    mode: str,
    snapshot_source: str | None,
    snapshot_coverage: float | None,
    quote_result: QuoteSnapshotResult | None = None,
) -> dict[str, object]:
    return {
        "mode": mode,
        "as_of": context.as_of,
        "price_as_of": context.price_as_of,
        "metadata_as_of": context.metadata_as_of,
        "trend_profile": context.trend_profile,
        "data_status": context.data_status,
        "snapshot_source": snapshot_source,
        "snapshot_coverage": snapshot_coverage,
        "snapshot_provider": quote_result.provider if quote_result is not None else None,
        "snapshot_status": quote_result.status if quote_result is not None else None,
        "snapshot_observed_at": quote_result.observed_at if quote_result is not None else None,
        "snapshot_raw_rows": quote_result.raw_rows if quote_result is not None else None,
        "snapshot_normalized_rows": quote_result.normalized_rows if quote_result is not None else None,
        "snapshot_errors": list(quote_result.errors) if quote_result is not None else [],
        "snapshot_from_cache": quote_result.from_cache if quote_result is not None else False,
        "snapshot_retry_at": quote_result.retry_at if quote_result is not None else None,
    }


def _date_alignment_evidence(
    context,
    *,
    selected_trade_date: str | None,
    formal_trade_date: str | None,
) -> dict[str, object]:
    dates = {
        "selected_trade_date": str(selected_trade_date) if selected_trade_date is not None else None,
        "effective_trade_date": str(context.trade_date) if context.trade_date is not None else None,
        "formal_trade_date": str(formal_trade_date) if formal_trade_date is not None else None,
    }
    comparable = [value for value in dates.values() if value is not None]
    return {
        **dates,
        "date_status": "aligned" if len(comparable) == 3 and len(set(comparable)) == 1 else "date_mismatch",
    }


def _formal_batch_trade_date(conn, trade_date: str) -> str | None:
    batch = latest_complete_batch(conn, trade_date)
    if batch.code != "complete" or batch.batch_id is None:
        return None
    try:
        row = conn.execute(
            "SELECT trade_date FROM selection_batches WHERE batch_id=?",
            (batch.batch_id,),
        ).fetchone()
    except sqlite3.DatabaseError:
        return None
    return str(row[0]) if row is not None else None


def _apply_date_evidence_gate(
    rows: pd.DataFrame,
    status: pd.DataFrame,
    evidence: dict[str, object],
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, object]]:
    if evidence.get("date_status") != "date_mismatch":
        return rows, status, evidence
    blocked = status.copy()
    if not blocked.empty:
        blocked["availability"] = "date_mismatch"
        blocked["n_candidates"] = 0
        blocked["error_msg"] = (
            f"selected={evidence.get('selected_trade_date')}; "
            f"effective={evidence.get('effective_trade_date')}; "
            f"formal={evidence.get('formal_trade_date')}"
        )
    return pd.DataFrame(), blocked, evidence


def _annotate_formal_rows(rows: pd.DataFrame, strategy_id: str, *, reference_column: str = "reference_price") -> pd.DataFrame:
    """Turn one v2 evaluator's ordered rows into the common A–E display contract."""
    if rows.empty:
        return pd.DataFrame()
    result = rows.copy().reset_index(drop=True)
    if reference_column not in result.columns:
        return pd.DataFrame()
    result["reference_price"] = pd.to_numeric(result[reference_column], errors="coerce")
    result = result[result["reference_price"].gt(0)].copy()
    if result.empty:
        return result
    definition = next(item for item in formal_definitions() if item.strategy_id == strategy_id)
    result["strategy_id"] = strategy_id
    result["version"] = definition.version
    result["trade_date"] = result.get("trade_date", pd.Series(pd.NA, index=result.index))
    result["sec_type"] = "stock"
    result["entry_price"] = result["reference_price"]
    result["rank"] = range(1, len(result) + 1)
    result["capability"] = definition.capability
    result["capability_label"] = definition.label
    result["subtype"] = definition.subtype
    return result


def _annotate_serialized_formal_rows(rows: pd.DataFrame) -> pd.DataFrame:
    if rows.empty:
        return rows
    definitions = {definition.strategy_id: definition for definition in formal_definitions()}
    result = rows[rows["strategy_id"].isin(definitions)].copy()
    if result.empty:
        return result
    result["reference_price"] = pd.to_numeric(
        result.get("reference_price", result["entry_price"]), errors="coerce"
    ).fillna(result["entry_price"])
    result["capability"] = result["strategy_id"].map(lambda value: definitions[str(value)].capability)
    result["capability_label"] = result["strategy_id"].map(lambda value: definitions[str(value)].label)
    result["subtype"] = result["strategy_id"].map(lambda value: definitions[str(value)].subtype)
    return result


def _live_formal_capability_rows(conn, context) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Re-evaluate A–E from one non-persistent runtime context for a snapshot/pending view."""
    output_frames: list[pd.DataFrame] = []
    status_rows: list[dict[str, object]] = []

    def append_evaluation(strategy_id: str, rows: pd.DataFrame, diagnostics: dict[str, object], *, reference_column: str = "reference_price") -> None:
        frame = _annotate_formal_rows(rows, strategy_id, reference_column=reference_column)
        if not frame.empty:
            output_frames.append(frame)
        definition = next(item for item in formal_definitions() if item.strategy_id == strategy_id)
        status_rows.append(
            {
                "strategy_id": strategy_id,
                "capability": definition.capability,
                "availability": "临时结果" if not frame.empty else "成功但 0 条",
                "n_candidates": len(frame),
                "run_at": context.as_of,
                "last_nonempty": None,
                "skipped_reason_counts": diagnostics.get("skipped_reason_counts", {}),
            }
        )

    def append_failure(strategy_id: str, exc: Exception) -> None:
        definition = next(item for item in formal_definitions() if item.strategy_id == strategy_id)
        status_rows.append(
            {
                "strategy_id": strategy_id,
                "capability": definition.capability,
                "availability": "运行失败",
                "n_candidates": 0,
                "run_at": context.as_of,
                "last_nonempty": None,
                "skipped_reason_counts": {"runtime_error": type(exc).__name__},
            }
        )

    try:
        strong = evaluate_strong_trend(context)
        append_evaluation("strong_trend", strong.rows, strong.diagnostics, reference_column="reference_price")
    except Exception as exc:
        append_failure("strong_trend", exc)

    try:
        compression_rows, compression_diagnostics = evaluate_compression_launch(context)
        append_evaluation("compression_launch", compression_rows, compression_diagnostics)
    except Exception as exc:
        append_failure("compression_launch", exc)

    try:
        momentum_rows, momentum_diagnostics = evaluate_momentum_anomaly(context)
        append_evaluation("momentum_anomaly", momentum_rows, momentum_diagnostics)
    except Exception as exc:
        append_failure("momentum_anomaly", exc)

    try:
        pullback = build_pullback_support(conn, context.trade_date, context=context)
        pullback_candidates = select_candidates_from_pullback_support(
            pullback,
            trade_date=context.trade_date,
            strategy_id="second_launch",
            version=next(item for item in formal_definitions() if item.strategy_id == "second_launch").version,
            top_n=20,
        )
        pullback_rows = _annotate_serialized_formal_rows(_serialize_candidates(pullback_candidates))
        if not pullback_rows.empty:
            output_frames.append(pullback_rows)
        status_rows.append(
            {
                "strategy_id": "second_launch",
                "capability": "C",
                "availability": "临时结果" if not pullback_rows.empty else "成功但 0 条",
                "n_candidates": len(pullback_rows),
                "run_at": context.as_of,
                "last_nonempty": None,
                "skipped_reason_counts": pullback.diagnostics.get("skipped_reason_counts", {}),
            }
        )
    except Exception as exc:
        append_failure("second_launch", exc)

    try:
        base_rows, base_diagnostics = evaluate_base_breakout(context)
        append_evaluation("base_breakout", base_rows, base_diagnostics)
    except Exception as exc:
        append_failure("base_breakout", exc)

    try:
        rs_rows, rs_diagnostics = evaluate_counter_trend_rs(context)
        append_evaluation("counter_trend_rs", rs_rows, rs_diagnostics)
    except Exception as exc:
        append_failure("counter_trend_rs", exc)
    combined = pd.concat(output_frames, ignore_index=True, sort=False) if output_frames else pd.DataFrame()
    if not combined.empty:
        selected_frames = []
        for _, frame in combined.groupby("capability", sort=True):
            selected = shortlist_capability_rows(frame)
            selected["rank"] = selected["capability_rank"]
            selected_frames.append(selected)
        combined = pd.concat(selected_frames, ignore_index=True, sort=False)
    return combined, pd.DataFrame(status_rows)


def _formal_capability_view(
    conn,
    trade_date: str,
    *,
    selected_trade_date: str | None = None,
    now: dt.datetime | None = None,
    runtime: SelectionRuntime | None = None,
    snapshot_loader: SnapshotLoader | None = None,
    intraday_enabled: bool | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, object]]:
    """Read a close-final batch, or evaluate one current-session provisional snapshot."""
    current = now or dt.datetime.now(CHINA_TZ)
    batch = latest_complete_batch(conn, trade_date)
    formal_trade_date = _formal_batch_trade_date(conn, trade_date)
    batch_pending = batch.code == "unfinalized"
    selected = str(selected_trade_date or trade_date)
    def waiting_view(reason: str | None = None) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, object]]:
        evidence: dict[str, object] = {
            "mode": MODE_CLOSE_PENDING,
            "result_kind": "waiting_for_formal_batch",
            "as_of": None,
            "price_as_of": str(trade_date),
            "metadata_as_of": None,
            "trend_profile": None,
            "data_status": "pending",
            "snapshot_source": None,
            "snapshot_coverage": None,
            "snapshot_provider": None,
            "snapshot_status": None,
            "snapshot_observed_at": None,
            "snapshot_raw_rows": None,
            "snapshot_normalized_rows": None,
            "snapshot_errors": [],
            "snapshot_from_cache": False,
            "snapshot_retry_at": None,
            "selected_trade_date": selected,
            "effective_trade_date": str(trade_date),
            "formal_trade_date": None,
            "date_status": "batch_pending",
            "formal_batch_status": "pending" if batch_pending else batch.code,
            "runtime_reason": reason,
        }
        status = _capability_run_status(conn, trade_date)
        if not status.empty:
            status["availability"] = "waiting_for_formal_batch"
            if reason is not None:
                status["error_msg"] = reason
        status.attrs["a_history_coverage"] = a_history_coverage(conn, trade_date)
        return pd.DataFrame(), status, evidence

    if batch.code == "complete":
        evidence = {
            "mode": MODE_CLOSE_FINAL,
            "result_kind": "persisted",
            "as_of": None,
            "price_as_of": str(trade_date),
            "metadata_as_of": None,
            "trend_profile": None,
            "data_status": "complete",
            "snapshot_source": None,
            "snapshot_coverage": None,
            "snapshot_provider": None,
            "snapshot_status": None,
            "snapshot_observed_at": None,
            "snapshot_raw_rows": None,
            "snapshot_normalized_rows": None,
            "snapshot_errors": [],
            "snapshot_from_cache": False,
            "snapshot_retry_at": None,
            "selected_trade_date": selected,
            "effective_trade_date": str(trade_date),
            "formal_trade_date": formal_trade_date,
            "date_status": "aligned" if selected == str(trade_date) == formal_trade_date else "date_mismatch",
            "formal_batch_status": "complete",
        }
        status = _capability_run_status(conn, trade_date)
        status.attrs["a_history_coverage"] = a_history_coverage(conn, trade_date)
        return _apply_date_evidence_gate(
            _load_formal_capability_candidates(conn, trade_date), status, evidence
        )

    quality = inspect_stock_session(conn, trade_date)
    if str(quality.get("status")) in {STATUS_CLEAN, STATUS_USABLE_WITH_QUARANTINE}:
        return waiting_view("complete_close_batch_required")

    current_cn = current.replace(tzinfo=CHINA_TZ) if current.tzinfo is None else current.astimezone(CHINA_TZ)
    current_time = current_cn.time().replace(tzinfo=None)
    in_session = (
        current_cn.date().isoformat() == str(trade_date)
        and current_cn.weekday() < 5
        and ((dt.time(9, 30) <= current_time <= dt.time(11, 30)) or (dt.time(13, 0) <= current_time < dt.time(15, 0)))
    )
    if intraday_enabled is not True or not in_session or snapshot_loader is None:
        return waiting_view("current_intraday_snapshot_required")

    quote_result = _quote_result(snapshot_loader())
    if quote_result.observed_at is None:
        quote_result = replace(quote_result, observed_at=current_cn.isoformat())
    snapshot = quote_result.frame.copy() if quote_result.status in {"snapshot_usable", "snapshot_cache_fallback"} else pd.DataFrame()
    result = (runtime or SelectionRuntime()).run(
        conn,
        trade_date,
        now=current_cn,
        snapshot_bars=snapshot,
        snapshot_benchmark_closes=quote_result.benchmark_closes,
        snapshot_as_of=quote_result.observed_at if not snapshot.empty else None,
        snapshot_source=quote_result.provider,
    )
    context = result.context
    if context is None or result.mode != MODE_INTRADAY:
        return waiting_view(result.reason)
    clean_dates = {str(value) for value in context.diagnostics.get("clean_dates", [])}
    # The selection context does not yet carry a local authoritative A-share
    # calendar.  Never replace that missing truth with a weekday approximation.
    expected_prior = context.diagnostics.get("expected_prior_trade_date")
    if expected_prior is None or str(expected_prior) not in clean_dates:
        rows, status, evidence = waiting_view(
            "stale_history" if expected_prior is not None else "expected_trade_calendar_unavailable"
        )
        if not status.empty:
            status["availability"] = "stale_history"
        evidence["history_status"] = "stale_history"
        evidence["expected_prior_trade_date"] = str(expected_prior) if expected_prior is not None else None
        evidence["latest_formal_trade_date"] = _persisted_candidate_dates(conn)[-1] if _persisted_candidate_dates(conn) else None
        return rows, status, evidence

    evidence = _selection_evidence(
        context,
        mode=MODE_INTRADAY,
        snapshot_source=result.snapshot_source,
        snapshot_coverage=result.snapshot_coverage,
        quote_result=quote_result,
    )
    evidence.update(
        {
            "result_kind": "provisional",
            "formal_batch_status": "provisional",
            "selected_trade_date": selected,
            "effective_trade_date": str(context.trade_date),
            "formal_trade_date": str(context.trade_date),
            "date_status": "aligned" if selected == str(context.trade_date) else "date_mismatch",
            "benchmark_status": quote_result.benchmark_status,
            "benchmark_provider": quote_result.benchmark_provider,
            "benchmark_observed_at": quote_result.benchmark_observed_at,
            "benchmark_errors": list(quote_result.benchmark_errors),
        }
    )
    rows, status = _live_formal_capability_rows(conn, context)
    if not rows.empty:
        rows["result_kind"] = "provisional"
    if not status.empty:
        status["availability"] = "provisional"
    status.attrs["a_history_coverage"] = a_history_coverage(
        conn,
        trade_date,
        usable_dates=context.diagnostics.get("usable_dates"),
    )
    return _apply_date_evidence_gate(rows, status, evidence)


def ensure_close_history_persisted(
    base_dir: str | Path,
    up_to_trade_date: str | None,
    limit: int = 10,
) -> dict[str, object]:
    """Explicit bounded recent-date catch-up; panel reads and the R4 60-day bootstrap do not call this."""
    if up_to_trade_date is None:
        return {"processed": [], "latest_persisted": None}

    conn = connect(base_dir=base_dir)
    try:
        close_dates = list_stock_trade_dates(conn, end_date=up_to_trade_date, include_end=True)
        schema = selection_batch_schema_state(conn)
        stored_dates = set(
            _persisted_candidate_dates(conn)
            if schema.ready
            else _legacy_persisted_candidate_dates(conn)
        )
    finally:
        conn.close()

    recent_close_dates = close_dates[-limit:] if limit > 0 else close_dates
    missing_dates = [day for day in recent_close_dates if day not in stored_dates]

    processed: list[str] = []
    for trade_date in missing_dates:
        execute_daily_pipeline(
            base_dir=base_dir,
            trade_date=trade_date,
            refresh=False,
            emit_reports=False,
        )
        processed.append(trade_date)

    conn = connect(base_dir=base_dir)
    try:
        schema = selection_batch_schema_state(conn)
        latest_persisted = (
            _persisted_candidate_dates(conn)
            if schema.ready
            else _legacy_persisted_candidate_dates(conn)
        )
    finally:
        conn.close()
    return {
        "processed": processed,
        "latest_persisted": latest_persisted[-1] if latest_persisted else None,
    }


def _can_use_latest_quotes(
    conn,
    trade_date: str | None,
    use_intraday: bool,
    prefer_latest_quotes: bool,
    force_latest_quotes: bool,
    fallback_trade_date: str | None,
) -> bool:
    if not trade_date:
        return False
    if use_intraday:
        return True
    if force_latest_quotes:
        return True
    if not prefer_latest_quotes:
        return False
    close_trade_date = _resolve_close_trade_date(conn, fallback_trade_date or trade_date)
    if close_trade_date is None:
        return True
    return str(close_trade_date) < str(trade_date)


def _load_latest_quote_opportunities(
    conn,
    trade_date: str,
    snapshot_loader: SnapshotLoader | None = None,
) -> tuple[pd.DataFrame, bool]:
    try:
        latest_quotes = _snapshot_frame(snapshot_loader()) if snapshot_loader is not None else pd.DataFrame()
    except Exception:
        return pd.DataFrame(), False
    if latest_quotes.empty:
        return pd.DataFrame(), False
    return _run_display_scanners_for_snapshot(conn, trade_date, _snapshot_universe(conn, trade_date, latest_quotes)), True


def _resolve_close_trade_date(conn, trade_date: str | None) -> str | None:
    dates = list_stock_trade_dates(conn, end_date=trade_date, limit=1, include_end=True)
    return dates[-1] if dates else None


def _resolve_previous_trade_date(conn, trade_date: str) -> str | None:
    dates = list_stock_trade_dates(conn, end_date=trade_date, limit=1, include_end=False)
    return dates[-1] if dates else None


def _load_close_price_map(conn, trade_date: str, sec_codes: list[str]) -> dict[str, float]:
    if not sec_codes:
        return {}
    placeholders = ",".join("?" for _ in sec_codes)
    rows = conn.execute(
        f"""
        SELECT sec_code, close
        FROM ash.kline_daily
        WHERE sec_type='stock' AND trade_date=? AND sec_code IN ({placeholders})
        """,
        [trade_date, *sec_codes],
    ).fetchall()
    return {_canonical_code(row[0]): _safe_float(row[1]) for row in rows}


def _load_latest_quote_price_map(
    snapshot_loader: SnapshotLoader | None = None,
    sec_codes: list[str] | None = None,
) -> dict[str, float]:
    try:
        latest_quotes = _snapshot_frame(snapshot_loader()) if snapshot_loader is not None else pd.DataFrame()
    except Exception:
        return {}
    if latest_quotes.empty:
        return {}
    if sec_codes:
        wanted = {_canonical_code(code) for code in sec_codes}
        latest_quotes = latest_quotes[latest_quotes["sec_code"].isin(wanted)].copy()
        if latest_quotes.empty:
            return {}
    return (
        latest_quotes[["sec_code", "close"]]
        .drop_duplicates(subset=["sec_code"])
        .set_index("sec_code")["close"]
        .to_dict()
    )


def _build_followups(
    previous_df: pd.DataFrame,
    latest_prices: dict[str, float],
    status: str,
) -> pd.DataFrame:
    if previous_df.empty or "sec_code" not in previous_df.columns:
        return pd.DataFrame()

    follow = previous_df[["strategy_id", "sec_code", "sec_name", "rank", "entry_price"]].copy()
    follow["latest_price"] = pd.to_numeric(follow["sec_code"].map(latest_prices), errors="coerce")
    entry_price = pd.to_numeric(follow["entry_price"], errors="coerce")
    valid = entry_price.gt(0) & follow["latest_price"].notna()
    change_pct = ((follow["latest_price"] - entry_price) / entry_price * 100.0).where(valid)
    follow["today_change_pct"] = pd.to_numeric(change_pct, errors="coerce").astype("float64")
    follow["status"] = status
    return follow.sort_values(["strategy_id", "rank", "sec_code"]).reset_index(drop=True)


def load_latest_opportunities(
    base_dir: str | Path,
    trade_date: str | None = None,
    use_intraday: bool = False,
    prefer_latest_quotes: bool = False,
    force_latest_quotes: bool = False,
    fallback_trade_date: str | None = None,
    snapshot_loader: SnapshotLoader | None = None,
) -> tuple[pd.DataFrame, str | None]:
    conn = connect(base_dir=base_dir)
    try:
        should_try_latest_quotes = _can_use_latest_quotes(
            conn=conn,
            trade_date=trade_date,
            use_intraday=use_intraday,
            prefer_latest_quotes=prefer_latest_quotes,
            force_latest_quotes=force_latest_quotes,
            fallback_trade_date=fallback_trade_date,
        )
        if should_try_latest_quotes:
            today_df, has_latest_quotes = _load_latest_quote_opportunities(
                conn, trade_date, snapshot_loader=snapshot_loader
            )
            if has_latest_quotes and (force_latest_quotes or not today_df.empty):
                return today_df, trade_date

        close_trade_date = _resolve_close_trade_date(conn, fallback_trade_date or trade_date)
        if close_trade_date is None:
            return pd.DataFrame(), None
        return _load_close_opportunities(conn, close_trade_date), close_trade_date
    finally:
        conn.close()


def load_previous_day_followups(
    base_dir: str | Path,
    trade_date: str | None = None,
    use_intraday: bool = False,
    prefer_latest_quotes: bool = False,
    force_latest_quotes: bool = False,
    fallback_trade_date: str | None = None,
    snapshot_loader: SnapshotLoader | None = None,
) -> tuple[pd.DataFrame, str | None, str | None]:
    conn = connect(base_dir=base_dir)
    try:
        should_try_latest_quotes = _can_use_latest_quotes(
            conn=conn,
            trade_date=trade_date,
            use_intraday=use_intraday,
            prefer_latest_quotes=prefer_latest_quotes,
            force_latest_quotes=force_latest_quotes,
            fallback_trade_date=fallback_trade_date,
        )
        if should_try_latest_quotes:
            persisted_dates = _persisted_candidate_dates(conn)
            anchor_date = None
            reference_date = trade_date or fallback_trade_date
            if reference_date:
                valid = [day for day in persisted_dates if day < reference_date]
                anchor_date = valid[-1] if valid else None
            if anchor_date is None and persisted_dates:
                valid = [day for day in persisted_dates if trade_date is None or day < trade_date]
                anchor_date = valid[-1] if valid else None
            if anchor_date is None:
                anchor_date = _resolve_previous_trade_date(conn, reference_date or trade_date)
            previous_date = anchor_date
            if previous_date is not None:
                previous_df = _load_persisted_candidates(conn, previous_date)
                if previous_df.empty:
                    previous_df = _load_close_opportunities(conn, previous_date)
                sec_codes = (
                    previous_df["sec_code"].tolist()
                    if (not previous_df.empty and "sec_code" in previous_df.columns)
                    else []
                )
                latest_prices = _load_latest_quote_price_map(
                    snapshot_loader=snapshot_loader,
                    sec_codes=sec_codes,
                )
                if latest_prices:
                    status = "盘中快照" if use_intraday else "最新价格"
                    return _build_followups(previous_df, latest_prices, status), previous_date, trade_date

        current_date = _resolve_close_trade_date(conn, fallback_trade_date or trade_date)
        if current_date is None:
            return pd.DataFrame(), None, None
        persisted_dates = _persisted_candidate_dates(conn)
        valid = [day for day in persisted_dates if day < current_date]
        previous_date = valid[-1] if valid else None
        if previous_date is None:
            previous_date = _resolve_previous_trade_date(conn, current_date)
        if previous_date is None:
            return pd.DataFrame(), None, current_date
        previous_df = _load_persisted_candidates(conn, previous_date)
        if previous_df.empty:
            previous_df = _load_close_opportunities(conn, previous_date)
        sec_codes = (
            previous_df["sec_code"].tolist()
            if (not previous_df.empty and "sec_code" in previous_df.columns)
            else []
        )
        latest_prices = _load_close_price_map(conn, current_date, sec_codes)
        return _build_followups(previous_df, latest_prices, "日线收盘"), previous_date, current_date
    finally:
        conn.close()


def _prepare_today_display(today_df: pd.DataFrame) -> pd.DataFrame:
    df = today_df.copy()
    for column in (
        "change_pct",
        "high_over_open_pct",
        "high_over_close_pct",
        "score",
        "path",
        "rank",
    ):
        if column not in df.columns:
            df[column] = pd.NA
    df = df.rename(
        columns={
            "strategy_id": "来源",
            "sec_code": "代码",
            "sec_name": "名称",
            "change_pct": "涨幅%",
            "high_over_open_pct": "最高比开盘%",
            "high_over_close_pct": "最高比收盘%",
            "score": "强度分",
            "path": "路径",
            "rank": "排名",
        }
    )
    df["来源"] = df["来源"].map(
        {
            "momentum_breakout": "异动",
            "rps_stock_top20": "强势",
            "trend_embryo": "强趋势胚子",
            "true_leader": "真龙/中军",
            "second_launch": "二次启动低吸",
            "launch_burst": "主升启动",
        }
    ).fillna(df["来源"])
    return df[
        [
            "来源",
            "排名",
            "代码",
            "名称",
            "涨幅%",
            "最高比开盘%",
            "最高比收盘%",
            "强度分",
            "路径",
        ]
    ]


def _prepare_followup_display(follow_df: pd.DataFrame) -> pd.DataFrame:
    df = follow_df.copy()
    for column in ("rank", "today_change_pct", "latest_price", "status"):
        if column not in df.columns:
            df[column] = pd.NA
    df = df.rename(
        columns={
            "strategy_id": "来源",
            "sec_code": "代码",
            "sec_name": "名称",
            "rank": "排名",
            "today_change_pct": "今日涨幅%",
            "latest_price": "最新价",
            "status": "口径",
        }
    )
    df["来源"] = df["来源"].map(
        {
            "momentum_breakout": "异动",
            "rps_stock_top20": "强势",
            "trend_embryo": "强趋势胚子",
            "true_leader": "真龙/中军",
            "second_launch": "二次启动低吸",
            "launch_burst": "主升启动",
        }
    ).fillna(df["来源"])
    return df[["来源", "排名", "代码", "名称", "今日涨幅%", "最新价", "口径"]]


def _prepare_launch_display(launch_df: pd.DataFrame) -> pd.DataFrame:
    df = launch_df.copy()
    for column in ("rank", "pct", "volume_ratio", "sigma_multiple", "cluster_days", "cluster_width"):
        if column not in df.columns:
            df[column] = pd.NA
    df["pct"] = pd.to_numeric(df["pct"], errors="coerce") * 100.0
    df["cluster_width"] = pd.to_numeric(df["cluster_width"], errors="coerce") * 100.0
    df = df.rename(
        columns={
            "rank": "排名",
            "sec_code": "代码",
            "sec_name": "名称",
            "pct": "涨幅%",
            "volume_ratio": "量比",
            "sigma_multiple": "σ倍数",
            "cluster_days": "横盘天数",
            "cluster_width": "均线簇宽度%",
        }
    )
    return df[["排名", "代码", "名称", "涨幅%", "量比", "σ倍数", "横盘天数", "均线簇宽度%"]]


def _build_followup_label(base_dir: str | Path, previous_date: str | None, current_date: str | None) -> str:
    if not previous_date or not current_date:
        return "**昨日挖掘标的今日涨幅**"

    label = f"**昨日挖掘标的今日涨幅（{previous_date} -> {current_date}）**"
    expected_previous = None
    try:
        from app_panel import get_latest_cn_trade_date

        expected_previous_ts = get_latest_cn_trade_date(pd.Timestamp(current_date) - pd.Timedelta(days=1))
        if expected_previous_ts is not None:
            expected_previous = str(pd.Timestamp(expected_previous_ts).date())
    except Exception:
        expected_previous = None

    if expected_previous and previous_date != expected_previous:
        return f"**最近收盘挖掘标的最新涨幅（{previous_date} -> {current_date}）**"
    return label


def load_watchlist_snapshot(
    base_dir: str | Path,
    trade_date: str | None,
    lookback: int = 40,
) -> tuple[pd.DataFrame, str | None]:
    """Build the strong-stock funnel for the latest daily-bar date at or before trade_date.

    The panel's trade_date can be an intraday/latest-quote date with no daily kline yet
    (e.g. mid-session today). The funnel is derived purely from daily bars (MA10, pullback,
    volume-shrink), so it can only be computed up to the last completed daily bar. We resolve
    down to that date and return it so the label reflects the day actually computed.
    """
    if not trade_date:
        return pd.DataFrame(), None
    conn = connect(base_dir=base_dir)
    try:
        available = list_stock_trade_dates(conn, end_date=trade_date, limit=1, include_end=True)
        effective_date = available[-1] if available else None
        if not effective_date:
            return pd.DataFrame(), None
        return build_watchlist(conn, effective_date, lookback=lookback), effective_date
    finally:
        conn.close()


def _load_market_regime_summary(conn, trade_date: str | None) -> dict[str, object]:
    if not trade_date:
        return {"available": False, "message": "市场灯不可用：缺少交易日"}
    try:
        regime = compute_market_regime(conn, str(trade_date))
    except Exception as exc:
        return {"available": False, "message": f"市场灯不可用：{type(exc).__name__}"}
    light = str(regime.get("light") or "UNKNOWN")
    advancers_ratio = regime.get("advancers_ratio")
    median_pct = regime.get("median_pct")
    n = int(regime.get("n") or 0)
    if n <= 0 or advancers_ratio is None:
        return {"available": False, "light": light, "n": n, "message": "市场灯不可用：样本不足"}
    return {
        "available": True,
        "light": light,
        "advancers_ratio": float(advancers_ratio),
        "median_pct": float(median_pct) if median_pct is not None else math.nan,
        "n": n,
    }


def _format_market_regime(summary: dict[str, object]) -> str:
    if not summary.get("available"):
        return str(summary.get("message") or "市场灯不可用")
    light = str(summary.get("light") or "UNKNOWN")
    emoji = {"RED": "🔴", "YELLOW": "🟡", "GREEN": "🟢"}.get(light, "⚪")
    advancers = _safe_float(summary.get("advancers_ratio")) * 100.0
    median_pct = _safe_float(summary.get("median_pct")) * 100.0
    return (
        f"{emoji} {light} | 上涨家数占比 {advancers:.1f}% | "
        f"中位涨跌 {median_pct:.2f}% | 阈值：<40% RED，>60% GREEN"
    )


def _load_strategy_run_status(conn, trade_date: str | None) -> pd.DataFrame:
    columns = ["strategy_id", "status", "n_candidates", "universe_size", "run_at"]
    if not trade_date:
        return pd.DataFrame(columns=columns)
    try:
        df = pd.read_sql_query(
            """
            SELECT strategy_id, status, n_candidates, universe_size, run_at
            FROM strategy_runs
            WHERE trade_date=?
            ORDER BY strategy_id, run_at DESC, run_id DESC
            """,
            conn,
            params=[trade_date],
        )
    except Exception:
        return pd.DataFrame(columns=columns)
    if df.empty:
        return pd.DataFrame(columns=columns)
    return df.drop_duplicates("strategy_id", keep="first").reset_index(drop=True)


def _load_last_watchlist_state_date(conn, state: str) -> str | None:
    try:
        row = conn.execute(
            """
            SELECT MAX(snapshot_date)
            FROM watchlist_snapshots
            WHERE state=?
            """,
            (state,),
        ).fetchone()
    except Exception:
        return None
    return str(row[0]) if row and row[0] else None


def _watchlist_state_rows(watchlist_df: pd.DataFrame, state: str, top_n: int = 30) -> pd.DataFrame:
    if watchlist_df.empty or "state" not in watchlist_df.columns:
        return watchlist_df.iloc[0:0].copy()
    sorted_df = watchlist_df.sort_values("triage", ascending=False).reset_index(drop=True)
    return sorted_df[sorted_df["state"].eq(state)].head(top_n).reset_index(drop=True)


def _prepare_watchlist_display(watchlist_df: pd.DataFrame) -> pd.DataFrame:
    columns = [
        "代码",
        "名称",
        "状态",
        "原始强度%",
        "标记次数",
        "峰值回撤%",
        "缩量比",
        "距MA%",
        "今日站回MA10",
        "回踩大阴线",
        "距标记日",
        "来源策略",
    ]
    if watchlist_df.empty:
        return pd.DataFrame(columns=columns)
    df = watchlist_df.copy()
    for column in ("run_up_pct", "pullback_pct", "ma_proximity"):
        if column in df.columns:
            df[column] = pd.to_numeric(df[column], errors="coerce") * 100.0
    if "shrink_ratio" in df.columns:
        df["shrink_ratio"] = pd.to_numeric(df["shrink_ratio"], errors="coerce")
    display = df.rename(
        columns={
            "sec_code": "代码",
            "sec_name": "名称",
            "state": "状态",
            "run_up_pct": "原始强度%",
            "flag_count": "标记次数",
            "pullback_pct": "峰值回撤%",
            "shrink_ratio": "缩量比",
            "ma_proximity": "距MA%",
            "reclaim_ma10": "今日站回MA10",
            "pullback_red_days": "回踩大阴线",
            "days_since_flag": "距标记日",
            "flag_strategies": "来源策略",
        }
    )
    for column in columns:
        if column not in display.columns:
            display[column] = pd.NA
    return display[columns]

def _row_value(row: object, column: str) -> object:
    if isinstance(row, dict):
        return row.get(column)
    getter = getattr(row, "get", None)
    if callable(getter):
        return getter(column)
    return None


def _playbook_lookup_keys(row: object) -> list[str]:
    keys: list[str] = []

    def add(value: object) -> None:
        text = str(value or "").strip()
        if text and text not in keys:
            keys.append(text)

    add(_row_value(row, "state"))
    add(_row_value(row, "strategy_id"))
    flag_strategies = _row_value(row, "flag_strategies")
    if isinstance(flag_strategies, (list, tuple, set)):
        for item in flag_strategies:
            add(item)
    else:
        for item in str(flag_strategies or "").replace(";", ",").replace("，", ",").split(","):
            add(item)
    return keys


def _playbook_cards_for_row(
    row: object,
    lookup: Callable[[str], list[dict[str, object]]] = lookup_playbook,
) -> list[dict[str, object]]:
    cards: list[dict[str, object]] = []
    seen: set[str] = set()
    for key in _playbook_lookup_keys(row):
        try:
            matches = lookup(key)
        except Exception:
            matches = []
        for card in matches:
            card_key = str(card.get("id") or card.get("path") or card.get("title") or id(card))
            if card_key in seen:
                continue
            seen.add(card_key)
            cards.append(card)
    return cards


def _playbook_empty_message() -> str:
    return "该状态暂无复盘笔记模式"


def _render_card_section(title: str, value: object) -> None:
    st.markdown(f"**{title}**")
    text = str(value or "").strip()
    if text:
        st.markdown(text)
    else:
        st.caption("暂无")


def _render_raw_refs(card: dict[str, object]) -> None:
    st.markdown("**原文**")
    source_docs = card.get("source_docs")
    raw_paths = card.get("raw_paths") if isinstance(card.get("raw_paths"), dict) else {}
    docs = source_docs if isinstance(source_docs, list) else []
    if not docs:
        text = str(card.get("原文") or "").strip()
        if text:
            st.markdown(text)
        else:
            st.caption("暂无")
        return
    for doc_id in docs:
        doc_text = str(doc_id)
        raw_path = str(raw_paths.get(doc_text, "")) if isinstance(raw_paths, dict) else ""
        if raw_path:
            st.markdown(f"- {doc_text}: `{raw_path}`")
        else:
            st.markdown(f"- {doc_text}")


def _render_playbook_cards(cards: list[dict[str, object]]) -> None:
    if not cards:
        st.caption(_playbook_empty_message())
        return
    for idx, card in enumerate(cards):
        if idx:
            st.divider()
        st.markdown(f"**{card.get('title') or card.get('id') or '复盘笔记'}**")
        _render_card_section("模式定义", card.get("模式定义"))
        _render_card_section("看法", card.get("看法"))
        _render_card_section("案例", card.get("案例"))
        _render_raw_refs(card)


def _render_watchlist_playbooks(
    watchlist_df: pd.DataFrame,
    lookup: Callable[[str], list[dict[str, object]]] = lookup_playbook,
) -> None:
    if watchlist_df.empty:
        return
    for _, row in watchlist_df.iterrows():
        sec_code = str(row.get("sec_code") or "").strip()
        sec_name = str(row.get("sec_name") or "").strip()
        state = str(row.get("state") or "").strip()
        label_parts = [part for part in (sec_code, sec_name, state) if part]
        with st.expander(" · ".join(label_parts) or "复盘笔记", expanded=False):
            _render_playbook_cards(_playbook_cards_for_row(row, lookup=lookup))


def _render_legacy_observations(
    base_dir: str | Path,
    today_df: pd.DataFrame,
    today_date: str,
    follow_df: pd.DataFrame,
    previous_date: str | None,
    current_date: str | None,
    run_status_df: pd.DataFrame,
) -> None:
    """Keep pre-v2 observations available as an explicit, collapsed comparison surface."""
    st.markdown("**今日扫描运行状态**")
    if run_status_df.empty:
        st.caption("今日扫描未运行。")
    else:
        st.dataframe(
            run_status_df.rename(
                columns={
                    "strategy_id": "策略",
                    "status": "状态",
                    "n_candidates": "候选数",
                    "universe_size": "样本数",
                    "run_at": "运行时间",
                }
            ),
            width="stretch",
            hide_index=True,
        )

    legacy_today_df = (
        today_df[today_df["strategy_id"].isin(LEGACY_STRATEGY_IDS)].copy()
        if "strategy_id" in today_df.columns
        else pd.DataFrame()
    )
    launch_df = legacy_today_df[legacy_today_df["strategy_id"].eq("launch_burst")].copy()
    general_today_df = legacy_today_df[~legacy_today_df["strategy_id"].eq("launch_burst")].copy()
    st.markdown(f"**主升启动（初期异动，{today_date}）**")
    if launch_df.empty:
        st.info("当天没有主升启动信号。")
    else:
        st.dataframe(_prepare_launch_display(launch_df), width="stretch", hide_index=True)
        _render_watchlist_playbooks(launch_df)

    left, right = st.columns(2)
    with left:
        st.markdown(f"**当日挖掘标的（{today_date}）**")
        if general_today_df.empty:
            st.info("当天没有其他挖掘结果。")
        else:
            st.dataframe(_prepare_today_display(general_today_df), width="stretch", hide_index=True)

    with right:
        st.markdown(_build_followup_label(base_dir, previous_date, current_date))
        if follow_df.empty:
            st.info("昨天没有可展示的次日结果。")
        else:
            st.dataframe(_prepare_followup_display(follow_df), width="stretch", hide_index=True)

    watchlist_df, watchlist_date = load_watchlist_snapshot(base_dir=base_dir, trade_date=today_date)
    ready_df, trigger_df = split_actionable_watchlist(watchlist_df, top_n=30)
    extend_df = _watchlist_state_rows(watchlist_df, STATE_EXTEND, top_n=30)
    try:
        watchlist_conn = connect(base_dir=base_dir)
        try:
            last_ready_date = _load_last_watchlist_state_date(watchlist_conn, STATE_READY)
            last_trigger_date = _load_last_watchlist_state_date(watchlist_conn, STATE_RETRIGGER)
        finally:
            watchlist_conn.close()
    except Exception:
        last_ready_date = None
        last_trigger_date = None
    st.markdown(f"**强势股回踩与再启动跟踪（{watchlist_date or today_date}，近40日）**")
    ready_col, trigger_col = st.columns(2)
    with ready_col:
        st.markdown(f"**{STATE_READY}·预备（{len(ready_df)}）**")
        if ready_df.empty:
            st.info(f"暂无{STATE_READY}的预备标的。")
            st.caption(f"最近一次出现：{last_ready_date or '历史上未出现过'}")
        else:
            st.dataframe(_prepare_watchlist_display(ready_df), width="stretch", hide_index=True)
            _render_watchlist_playbooks(ready_df)
    with trigger_col:
        st.markdown(f"**{STATE_RETRIGGER}·触发今日（{len(trigger_df)}）**")
        if trigger_df.empty:
            st.info(f"暂无{STATE_RETRIGGER}触发标的。")
            st.caption(f"最近一次出现：{last_trigger_date or '历史上未出现过'}")
        else:
            st.dataframe(_prepare_watchlist_display(trigger_df), width="stretch", hide_index=True)
            _render_watchlist_playbooks(trigger_df)

    st.markdown(f"**持有管理·{STATE_EXTEND}（{len(extend_df)}）**")
    if extend_df.empty:
        st.info(f"暂无{STATE_EXTEND}持有管理标的。")
    else:
        st.dataframe(_prepare_watchlist_display(extend_df), width="stretch", hide_index=True)
        _render_watchlist_playbooks(extend_df)

    with st.expander("查看全部沉淀名单", expanded=False):
        if watchlist_df.empty:
            st.info("近40日还没有可沉淀的强势观察名单。")
        else:
            states = sorted(str(value) for value in watchlist_df["state"].dropna().unique()) if "state" in watchlist_df.columns else []
            selected_states = st.multiselect("状态筛选", states, default=states)
            full_df = watchlist_df[watchlist_df["state"].isin(selected_states)] if selected_states else watchlist_df.iloc[0:0]
            st.dataframe(_prepare_watchlist_display(full_df), width="stretch", hide_index=True)
def render_scanner_tab(
    base_dir: str | Path,
    fallback_trade_date: str | None = None,
    latest_quote_trade_date: str | None = None,
    use_intraday: bool = False,
    prefer_latest_quotes: bool = False,
    force_latest_quotes: bool = False,
    now: dt.datetime | None = None,
    isolated_acceptance: bool = False,
) -> None:
    query_trade_date = latest_quote_trade_date or fallback_trade_date
    shared_snapshot_loader = None
    formal_runtime = SelectionRuntime()
    if query_trade_date and (use_intraday or prefer_latest_quotes or force_latest_quotes):
        adapter_key = f"quote_snapshot_adapter_{query_trade_date}"
        if adapter_key not in st.session_state:
            st.session_state[adapter_key] = QuoteSnapshotAdapter(
                cache_dir=Path(base_dir) / "output" / "screening-v2-work" / "cache"
            )
        shared_snapshot_loader = _build_quote_snapshot_loader(
            base_dir,
            query_trade_date,
            adapter=st.session_state[adapter_key],
            now=now,
            cache_only=isolated_acceptance,
        )
    today_df, today_date = load_latest_opportunities(
        base_dir=base_dir,
        trade_date=query_trade_date,
        use_intraday=use_intraday,
        prefer_latest_quotes=prefer_latest_quotes,
        force_latest_quotes=force_latest_quotes,
        fallback_trade_date=fallback_trade_date,
        snapshot_loader=shared_snapshot_loader,
    )
    follow_df, previous_date, current_date = load_previous_day_followups(
        base_dir=base_dir,
        trade_date=query_trade_date,
        use_intraday=use_intraday,
        prefer_latest_quotes=prefer_latest_quotes,
        force_latest_quotes=force_latest_quotes,
        fallback_trade_date=fallback_trade_date,
        snapshot_loader=shared_snapshot_loader,
    )
    if today_date is None:
        st.info("还没有机会挖掘结果，先跑一次日任务。")
        return

    try:
        status_conn = connect(base_dir=base_dir)
        try:
            market_summary = _load_market_regime_summary(status_conn, today_date)
            run_status_df = _load_strategy_run_status(status_conn, today_date)
            formal_today_df, capability_status_df, selection_evidence = _formal_capability_view(
                status_conn,
                today_date,
                selected_trade_date=query_trade_date or today_date,
                now=now,
                runtime=formal_runtime,
                snapshot_loader=shared_snapshot_loader,
                intraday_enabled=use_intraday,
            )
            c_history = capability_status_df.attrs.get("a_history_coverage") or a_history_coverage(status_conn, today_date)
        finally:
            status_conn.close()
    except Exception:
        market_summary = {"available": False, "message": "市场灯不可用"}
        run_status_df = pd.DataFrame(columns=["strategy_id", "status", "n_candidates", "universe_size", "run_at"])
        formal_today_df = pd.DataFrame()
        capability_status_df = pd.DataFrame()
        c_history = {
            "covered_sessions": 0,
            "target_sessions": 60,
            "status": "schema_error",
            "error_msg": "unable to read A history coverage",
        }
        selection_evidence = {
            "selected_trade_date": query_trade_date,
            "effective_trade_date": today_date,
            "formal_trade_date": None,
            "date_status": "date_mismatch",
            "mode": "data_unavailable",
            "as_of": None,
            "price_as_of": None,
            "metadata_as_of": None,
            "trend_profile": None,
            "data_status": "data_unavailable",
            "snapshot_source": None,
            "snapshot_coverage": None,
        }

    st.markdown("**当时市场背景**")
    st.caption(_format_market_regime(market_summary))

    st.markdown("**筛选证据**")
    st.dataframe(pd.DataFrame([selection_evidence]), width="stretch", hide_index=True)
    st.caption(
        "日期对齐："
        f"selected={selection_evidence.get('selected_trade_date') or 'N/A'} ｜ "
        f"effective={selection_evidence.get('effective_trade_date') or 'N/A'} ｜ "
        f"formal trade_date={selection_evidence.get('formal_trade_date') or 'N/A'}"
    )
    if selection_evidence.get("date_status") == "date_mismatch":
        st.error("date_mismatch：选择日期、实际评估日期与正式批次日期不一致，候选表已隐藏。")
    if selection_evidence.get("formal_batch_status") == "pending":
        st.warning("等待正式批次：当日 v2.5 close_final/complete batch 尚不可用，候选为空。")
    elif selection_evidence["mode"] == "intraday_snapshot":
        st.warning("盘中临时结果：不会写入正式收盘候选或状态历史。")
    elif selection_evidence["mode"] == "close_pending":
        st.warning("收盘待定：正在展示最后一批临时快照，收盘日线通过质量检查后才会定版。")

    st.markdown("**正式筛选 A–E**")
    if not capability_status_df.empty:
        display_status = capability_status_df.copy()
        if "capability" in display_status.columns:
            c_mask = display_status["capability"].eq("C")
            display_status.loc[c_mask, "a_history_coverage"] = (
                f"{c_history['covered_sessions']}/{c_history['target_sessions']}"
            )
        st.dataframe(display_status, width="stretch", hide_index=True)
    for capability in ("A", "B", "C", "D", "E"):
        definitions = formal_definitions(capability)
        label = definitions[0].label if definitions else capability
        capability_status = (
            capability_status_df[capability_status_df["capability"].eq(capability)].copy()
            if not capability_status_df.empty and "capability" in capability_status_df.columns
            else pd.DataFrame()
        )
        capability_rows = (
            formal_today_df[formal_today_df["capability"].eq(capability)].copy()
            if not formal_today_df.empty and "capability" in formal_today_df.columns
            else pd.DataFrame()
        )
        st.markdown(f"**{capability} · {label}**")
        if capability == "C":
            coverage_label = f"a_history_coverage={c_history['covered_sessions']}/{c_history['target_sessions']}"
            if c_history["status"] == "complete":
                if int(c_history["covered_sessions"]) < int(c_history["target_sessions"]):
                    st.warning(f"{coverage_label}；bootstrapping/partial recall，已有候选保持展示。")
                else:
                    st.caption(coverage_label)
            else:
                st.warning(f"{coverage_label}；A 历史状态={c_history['status']}。")
        if not capability_status.empty:
            summaries = []
            for item in capability_status.itertuples(index=False):
                skipped = getattr(item, "skipped_reason_counts", None)
                summary = f"{getattr(item, 'strategy_id')}：{getattr(item, 'availability')}"
                if skipped:
                    summary += f"；跳过={skipped}"
                summaries.append(summary)
            st.caption(" | ".join(summaries))
        if capability_rows.empty:
            st.info("该能力暂无收盘定版候选；请结合上方运行状态区分未运行、失败、空榜或数据缺失。")
            continue
        if capability == "C":
            tier_values = ["全部", *sorted(capability_rows.get("strength_tier", pd.Series(dtype=str)).dropna().astype(str).unique())]
            state_values = ["全部", *sorted(capability_rows.get("state", pd.Series(dtype=str)).dropna().astype(str).unique())]
            first, second = st.columns(2)
            tier = first.selectbox("强度档", tier_values, key=f"c_tier_{today_date}")
            state = second.selectbox("阶段", state_values, key=f"c_state_{today_date}")
            capability_rows = _filter_pullback_strength_phase(capability_rows, tier, state)
        display_columns = ["rank", "sec_code", "sec_name", "reference_price", "strategy_id"]
        evidence_columns = [
            field
            for definition in definitions
            for field in (*definition.sort_keys, *definition.fields)
        ]
        for column in dict.fromkeys(evidence_columns):
            if column in capability_rows.columns and column not in display_columns:
                display_columns.append(column)
        st.dataframe(capability_rows[display_columns], width="stretch", hide_index=True)

    with st.expander("Legacy / 其他观察（对照）", expanded=False):
        _render_legacy_observations(
            base_dir,
            today_df,
            today_date,
            follow_df,
            previous_date,
            current_date,
            run_status_df,
        )
