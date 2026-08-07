from __future__ import annotations

import sqlite3
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from .data_quality import (
    STATUS_CLEAN,
    STATUS_PARTIAL,
    STATUS_UNAVAILABLE,
    STATUS_USABLE_WITH_QUARANTINE,
    inspect_stock_session,
    usable_stock_trade_dates,
)
from .features import board_kind, change_pct


MAX_MARKET_CAP = 5_000 * 100_000_000
MIN_LISTING_DAYS = 87
SELECTION_CODE_PREFIXES = (
    "000",
    "001",
    "002",
    "003",
    "300",
    "301",
    "600",
    "601",
    "603",
    "605",
    "688",
    "689",
)


@dataclass
class SelectionUniverseResult:
    """The v2 stock pool and the evidence needed to decide whether it is publishable."""

    rows: pd.DataFrame
    trade_date: str
    universe_count: int
    excluded_reason_counts: dict[str, int]
    metadata_as_of: str | None
    data_status: str
    price_status: str = "ready"
    metadata_fresh: bool = True
    metadata_coverage: float | None = None
    metadata_provisional: bool = False
    diagnostics: dict[str, Any] = field(default_factory=dict)


def _schema_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {
        str(row[1])
        for row in conn.execute(f"PRAGMA ash.table_info({table})").fetchall()
    }


def _metadata_as_of(conn: sqlite3.Connection) -> str | None:
    if "updated_at" not in _schema_columns(conn, "stock_info"):
        return None
    row = conn.execute("SELECT MAX(updated_at) FROM ash.stock_info WHERE updated_at IS NOT NULL").fetchone()
    if row is None or row[0] is None:
        return None
    parsed = pd.to_datetime(row[0], errors="coerce")
    return None if pd.isna(parsed) else parsed.date().isoformat()


def _history_metrics(
    conn: sqlite3.Connection,
    clean_dates: list[str],
    prior_dates: list[str],
) -> pd.DataFrame:
    if not clean_dates:
        return pd.DataFrame(columns=["sec_code", "clean_bar_count", "amount_mean_20d", "turnover_mean_20d"])

    clean_marks = ",".join("?" for _ in clean_dates)
    counts = pd.read_sql_query(
        f"""
        SELECT sec_code, COUNT(*) AS clean_bar_count
        FROM ash.kline_daily
        WHERE sec_type='stock' AND trade_date IN ({clean_marks})
        GROUP BY sec_code
        """,
        conn,
        params=clean_dates,
    )
    if not prior_dates:
        counts["amount_mean_20d"] = pd.NA
        counts["turnover_mean_20d"] = pd.NA
        return counts

    prior_marks = ",".join("?" for _ in prior_dates)
    liquidity = pd.read_sql_query(
        f"""
        SELECT
          sec_code,
          AVG(amount) AS amount_mean_20d,
          AVG(turnover_ratio) AS turnover_mean_20d
        FROM ash.kline_daily
        WHERE sec_type='stock' AND trade_date IN ({prior_marks})
        GROUP BY sec_code
        """,
        conn,
        params=prior_dates,
    )
    return counts.merge(liquidity, on="sec_code", how="left")


def _snapshot_name_map(snapshot_names: pd.DataFrame | Mapping[str, str] | None) -> dict[str, str]:
    if snapshot_names is None:
        return {}
    if isinstance(snapshot_names, Mapping):
        items = snapshot_names.items()
    else:
        frame = pd.DataFrame(snapshot_names)
        name_column = next(
            (column for column in ("sec_name", "名称", "股票简称", "证券简称") if column in frame.columns),
            None,
        )
        if "sec_code" not in frame.columns or name_column is None:
            return {}
        items = zip(frame["sec_code"], frame[name_column], strict=False)
    names: dict[str, str] = {}
    for code, name in items:
        normalized_code = str(code).zfill(6)
        normalized_name = str(name).strip() if name is not None and not pd.isna(name) else ""
        if normalized_name and normalized_name.lower() not in {"nan", "<na>", "none"}:
            names[normalized_code] = normalized_name
    return names


def _empty_selection_result(
    trade_date: str,
    *,
    metadata_as_of: str | None,
    data_status: str,
    diagnostics: dict[str, Any],
) -> SelectionUniverseResult:
    return SelectionUniverseResult(
        rows=pd.DataFrame(),
        trade_date=str(trade_date),
        universe_count=0,
        excluded_reason_counts={},
        metadata_as_of=metadata_as_of,
        data_status=data_status,
        price_status="unavailable" if data_status not in {"ready", "partial"} else data_status,
        diagnostics=diagnostics,
    )


def build_selection_universe(
    conn: sqlite3.Connection,
    trade_date: str,
    *,
    snapshot_names: pd.DataFrame | Mapping[str, str] | None = None,
    clean_dates: list[str] | None = None,
    current_bars: pd.DataFrame | None = None,
    allow_missing_activity: bool = False,
) -> SelectionUniverseResult:
    """Build the sole v2 A-share stock pool without historical metadata look-ahead.

    The incoming snapshot name is authoritative for intraday ST filtering.  Without
    it, a stale or missing successful ``stock_info`` refresh makes the result
    explicitly non-publishable instead of silently presenting an official list.
    """
    metadata_as_of = _metadata_as_of(conn)
    quality = (
        inspect_stock_session(conn, trade_date)
        if current_bars is None
        else {"trade_date": str(trade_date), "status": STATUS_UNAVAILABLE, "reasons": ["intraday_snapshot"]}
    )
    price_status = (
        "ready"
        if current_bars is not None
        else {
            STATUS_CLEAN: "ready",
            STATUS_USABLE_WITH_QUARANTINE: "ready",
            STATUS_PARTIAL: "partial",
        }.get(str(quality["status"]), "unavailable")
    )
    diagnostics: dict[str, Any] = {"session_quality": quality, "price_status": price_status}
    if price_status != "ready" and current_bars is None:
        return _empty_selection_result(
            trade_date,
            metadata_as_of=metadata_as_of,
            data_status=price_status,
            diagnostics=diagnostics,
        )

    if current_bars is None:
        df = pd.read_sql_query(
            """
            SELECT
              k.sec_code,
              s.name AS metadata_name,
              k.open,
              k.high,
              k.low,
              k.close,
              k.pre_close,
              k.change,
              k.change_pct,
              k.volume,
              k.amount,
              k.turnover_ratio
            FROM ash.kline_daily k
            LEFT JOIN ash.stock_info s ON s.sec_code = k.sec_code
            WHERE k.sec_type='stock' AND k.trade_date=?
            """,
            conn,
            params=[str(trade_date)],
        )
    else:
        df = pd.DataFrame(current_bars).copy()
        required = {"sec_code", "open", "high", "low", "close", "pre_close"}
        if not allow_missing_activity:
            required.update({"volume", "amount"})
        missing = required.difference(df.columns)
        if missing:
            raise ValueError(f"current selection bars require columns: {sorted(missing)}")
        for column in ("change", "change_pct", "turnover_ratio", "volume", "amount"):
            if column not in df.columns:
                df[column] = pd.NA
        snapshot_column = "sec_name" if "sec_name" in df.columns else None
        df["sec_code"] = df["sec_code"].astype(str).str.zfill(6)
        metadata = pd.read_sql_query("SELECT sec_code, name AS metadata_name FROM ash.stock_info", conn)
        metadata["sec_code"] = metadata["sec_code"].astype(str).str.zfill(6)
        if snapshot_column:
            df["snapshot_name"] = df[snapshot_column]
        else:
            df["snapshot_name"] = pd.NA
        df = df.drop(columns=[snapshot_column] if snapshot_column else [], errors="ignore").merge(
            metadata.drop_duplicates("sec_code", keep="last"), on="sec_code", how="left"
        )
    if df.empty:
        return _empty_selection_result(
            trade_date,
            metadata_as_of=metadata_as_of,
            data_status="data_unavailable",
            diagnostics=diagnostics,
        )

    df["sec_code"] = df["sec_code"].astype(str).str.zfill(6)
    snapshot_by_code = _snapshot_name_map(snapshot_names)
    if "snapshot_name" not in df.columns:
        df["snapshot_name"] = pd.NA
    df["snapshot_name"] = df["sec_code"].map(snapshot_by_code).fillna(df["snapshot_name"])
    metadata_name = df["metadata_name"].where(df["metadata_name"].notna(), pd.NA)
    snapshot_name = df["snapshot_name"].where(df["snapshot_name"].notna(), pd.NA)
    df["st_status_known"] = snapshot_name.notna() | metadata_name.notna()
    df["sec_name"] = snapshot_name.fillna(metadata_name).fillna(df["sec_code"])
    df["change_pct"] = df["change_pct"].fillna(df.apply(change_pct, axis=1))
    df["board"] = df["sec_code"].map(board_kind)

    resolved_clean_dates = (
        usable_stock_trade_dates(conn, end_date=str(trade_date))[0]
        if clean_dates is None
        else sorted({str(day) for day in clean_dates if str(day) <= str(trade_date)})
    )
    latest_clean_date = resolved_clean_dates[-1] if resolved_clean_dates else None
    prior_dates = [day for day in resolved_clean_dates if day < str(trade_date)][-20:]
    metrics = _history_metrics(conn, resolved_clean_dates, prior_dates)
    df = df.merge(metrics, on="sec_code", how="left")
    snapshot_name_count = int(df["snapshot_name"].notna().sum())
    metadata_coverage = float(df["st_status_known"].mean()) if len(df) else 0.0
    has_complete_snapshot_names = bool(len(df)) and snapshot_name_count == len(df)
    metadata_fresh = has_complete_snapshot_names or (
        metadata_as_of is not None and (latest_clean_date is None or metadata_as_of >= latest_clean_date)
    )
    metadata_provisional = not metadata_fresh
    diagnostics.update(
        {
            "latest_clean_trade_date": latest_clean_date,
            "liquidity_history_dates": prior_dates,
            "snapshot_name_count": snapshot_name_count,
            "metadata_fresh": metadata_fresh,
            "metadata_coverage": metadata_coverage,
            "metadata_provisional": metadata_provisional,
        }
    )

    excluded = Counter()
    eligible = pd.Series(True, index=df.index)

    def exclude(mask: pd.Series, reason: str) -> None:
        nonlocal eligible
        rejected = eligible & mask.fillna(False)
        rejected_count = int(rejected.sum())
        if rejected_count:
            excluded[reason] += rejected_count
        eligible &= ~rejected

    exclude(~df["sec_code"].str.startswith(SELECTION_CODE_PREFIXES), "non_a_share_prefix")
    exclude(
        df["sec_name"].astype(str).str.contains(r"^\s*(?:\*?ST|S\*?ST)(?![A-Za-z])", case=False, regex=True),
        "st_name",
    )
    volume = pd.to_numeric(df["volume"], errors="coerce")
    amount = pd.to_numeric(df["amount"], errors="coerce")
    active = volume.gt(0) & amount.gt(0)
    activity_available = volume.notna() & amount.notna()
    exclude((activity_available & ~active) if allow_missing_activity else ~active, "no_valid_activity")

    result = df.loc[eligible].drop(columns=["metadata_name", "snapshot_name"]).reset_index(drop=True)
    result["activity_available"] = activity_available.loc[eligible].to_numpy()
    for metric in ("amount_mean_20d", "turnover_mean_20d"):
        result[f"{metric}_pct"] = result[metric].rank(pct=True, method="average")
    result["liquidity_pct"] = result["turnover_mean_20d_pct"].where(
        result["turnover_mean_20d_pct"].notna(), result["amount_mean_20d_pct"]
    )
    diagnostics["activity_unavailable_count"] = int((~activity_available.loc[eligible]).sum())
    if metadata_provisional:
        diagnostics["metadata_note"] = "metadata_provisional_last_known_name_filter"
    return SelectionUniverseResult(
        rows=result,
        trade_date=str(trade_date),
        universe_count=len(result),
        excluded_reason_counts=dict(sorted(excluded.items())),
        metadata_as_of=metadata_as_of,
        data_status=price_status,
        price_status=price_status,
        metadata_fresh=metadata_fresh,
        metadata_coverage=metadata_coverage,
        metadata_provisional=metadata_provisional,
        diagnostics=diagnostics,
    )


def build_universe(conn: sqlite3.Connection, trade_date: str) -> pd.DataFrame:
    df = pd.read_sql_query(
        """
        SELECT
          k.sec_code,
          COALESCE(s.name, k.sec_code) AS sec_name,
          k.open,
          k.high,
          k.low,
          k.close,
          k.pre_close,
          k.change,
          k.change_pct,
          k.volume,
          k.amount,
          k.turnover_ratio,
          m.total_mv,
          l.list_date
        FROM ash.kline_daily k
        LEFT JOIN ash.stock_info s ON s.sec_code = k.sec_code
        LEFT JOIN stock_market_cap m ON m.sec_code = k.sec_code
        LEFT JOIN stock_listing l ON l.sec_code = k.sec_code
        WHERE k.sec_type='stock'
          AND k.trade_date=?
          AND k.amount > 0
          AND k.volume > 0
        """,
        conn,
        params=[trade_date],
    )
    if df.empty:
        return df

    df["sec_code"] = df["sec_code"].astype(str).str.zfill(6)
    df["sec_name"] = df["sec_name"].fillna(df["sec_code"])
    df["change_pct"] = df["change_pct"].fillna(df.apply(change_pct, axis=1))
    df["board"] = df["sec_code"].map(board_kind)
    df["mv_missing"] = df["total_mv"].isna()
    df["listing_missing"] = df["list_date"].isna()

    df = df[~df["sec_code"].str.startswith(("8", "4", "9"))].copy()
    df = df[
        ~(
            (df["open"] == df["high"])
            & (df["high"] == df["low"])
            & (df["low"] == df["close"])
        )
    ].copy()
    df = df[~df["sec_name"].astype(str).str.upper().str.contains("ST", na=False)].copy()
    df = df[(df["total_mv"].isna()) | (df["total_mv"] <= MAX_MARKET_CAP)].copy()

    listing_dates = pd.to_datetime(df["list_date"], errors="coerce")
    age_days = (pd.Timestamp(trade_date) - listing_dates).dt.days
    df = df[df["listing_missing"] | (age_days >= MIN_LISTING_DAYS)].copy()

    return df.reset_index(drop=True)
