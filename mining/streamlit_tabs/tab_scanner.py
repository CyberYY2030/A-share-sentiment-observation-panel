from __future__ import annotations

import math
import json
from pathlib import Path
from typing import Callable

import pandas as pd
import streamlit as st

from ..db import connect, list_stock_trade_dates
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
from ..watchlist import build_watchlist, split_actionable_watchlist
from ..playbook import lookup_playbook
from run_daily import execute_daily_pipeline


SnapshotLoader = Callable[[], pd.DataFrame]


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


def _fetch_latest_quotes() -> pd.DataFrame:
    import akshare as ak  # type: ignore

    loaders = [
        getattr(ak, "stock_zh_a_spot", None),
        getattr(ak, "stock_zh_a_spot_em", None),
    ]
    for loader in loaders:
        if loader is None:
            continue
        try:
            df = loader()
            normalized = _normalize_snapshot_quotes(pd.DataFrame(df))
            if not normalized.empty:
                return normalized
        except Exception:
            continue
    return pd.DataFrame()


def _build_cached_snapshot_loader(snapshot_loader: SnapshotLoader | None = None) -> SnapshotLoader:
    cached: pd.DataFrame | None = None

    def _loader() -> pd.DataFrame:
        nonlocal cached
        if cached is None:
            cached = snapshot_loader() if snapshot_loader is not None else _fetch_latest_quotes()
        return cached.copy()

    return _loader


def _snapshot_universe(conn, trade_date: str, latest_quotes: pd.DataFrame) -> pd.DataFrame:
    previous_dates = list_stock_trade_dates(conn, end_date=trade_date, limit=1, include_end=False)
    reference_date = previous_dates[-1] if previous_dates else _resolve_close_trade_date(conn, trade_date)
    if reference_date is None:
        return pd.DataFrame()

    reference_universe = build_universe(conn, reference_date)
    if reference_universe.empty:
        return pd.DataFrame()

    keep = reference_universe[["sec_code", "sec_name"]].drop_duplicates()
    universe = keep.merge(latest_quotes, on="sec_code", how="inner")
    if universe.empty:
        return universe

    universe["sec_name"] = universe["sec_name"].fillna(universe["sec_code"])
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
    persisted = _load_persisted_candidates(conn, trade_date)
    if not persisted.empty:
        return persisted
    return _run_display_scanners(conn, trade_date, build_universe(conn, trade_date))


def _persisted_candidate_dates(conn) -> list[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT trade_date
        FROM candidates
        WHERE sec_type='stock'
          AND strategy_id IN ('momentum_breakout', 'rps_stock_top20', 'trend_embryo', 'true_leader', 'second_launch')
        ORDER BY trade_date
        """
    ).fetchall()
    return [str(row[0]) for row in rows]


def _load_persisted_candidates(conn, trade_date: str) -> pd.DataFrame:
    df = pd.read_sql_query(
        """
        SELECT strategy_id, trade_date, sec_type, sec_code, sec_name, entry_price, rank, features_json
        FROM candidates
        WHERE sec_type='stock'
          AND strategy_id IN ('momentum_breakout', 'rps_stock_top20', 'trend_embryo', 'true_leader', 'second_launch')
          AND trade_date=?
        ORDER BY strategy_id, rank, sec_code
        """,
        conn,
        params=[trade_date],
    )
    if df.empty:
        return df
    features = pd.json_normalize(df["features_json"].map(lambda text: json.loads(text or "{}")))
    return pd.concat([df.drop(columns=["features_json"]), features], axis=1)


def ensure_close_history_persisted(
    base_dir: str | Path,
    up_to_trade_date: str | None,
    limit: int = 10,
) -> dict[str, object]:
    if up_to_trade_date is None:
        return {"processed": [], "latest_persisted": None}

    conn = connect(base_dir=base_dir)
    try:
        close_dates = list_stock_trade_dates(conn, end_date=up_to_trade_date, include_end=True)
        stored_dates = set(_persisted_candidate_dates(conn))
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
        latest_persisted = _persisted_candidate_dates(conn)
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
        latest_quotes = (
            _normalize_snapshot_quotes(snapshot_loader())
            if snapshot_loader is not None
            else _fetch_latest_quotes()
        )
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
        latest_quotes = (
            _normalize_snapshot_quotes(snapshot_loader())
            if snapshot_loader is not None
            else _fetch_latest_quotes()
        )
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
    follow["latest_price"] = follow["sec_code"].map(latest_prices)
    valid = (
        pd.to_numeric(follow["entry_price"], errors="coerce").gt(0)
        & pd.to_numeric(follow["latest_price"], errors="coerce").notna()
    )
    follow["today_change_pct"] = math.nan
    follow.loc[valid, "today_change_pct"] = (
        (follow.loc[valid, "latest_price"] - follow.loc[valid, "entry_price"])
        / follow.loc[valid, "entry_price"]
        * 100.0
    )
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
        }
    ).fillna(df["来源"])
    return df[["来源", "排名", "代码", "名称", "今日涨幅%", "最新价", "口径"]]


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
) -> pd.DataFrame:
    if not trade_date:
        return pd.DataFrame()
    conn = connect(base_dir=base_dir)
    try:
        return build_watchlist(conn, trade_date, lookback=lookback)
    finally:
        conn.close()


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
def render_scanner_tab(
    base_dir: str | Path,
    fallback_trade_date: str | None = None,
    latest_quote_trade_date: str | None = None,
    use_intraday: bool = False,
    prefer_latest_quotes: bool = False,
    force_latest_quotes: bool = False,
) -> None:
    if fallback_trade_date:
        sync_key = f"scanner_close_sync_{fallback_trade_date}"
        sync_state = st.session_state.get(sync_key)
        if sync_state != fallback_trade_date:
            ensure_close_history_persisted(base_dir=base_dir, up_to_trade_date=fallback_trade_date)
            st.session_state[sync_key] = fallback_trade_date

    query_trade_date = latest_quote_trade_date or fallback_trade_date
    shared_snapshot_loader = (
        _build_cached_snapshot_loader()
        if query_trade_date and (use_intraday or prefer_latest_quotes or force_latest_quotes)
        else None
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

    left, right = st.columns(2)
    with left:
        st.markdown(f"**当日挖掘标的（{today_date}）**")
        if today_df.empty:
            st.info("当天没有挖掘结果。")
        else:
            st.dataframe(_prepare_today_display(today_df), width="stretch", hide_index=True)

    with right:
        st.markdown(_build_followup_label(base_dir, previous_date, current_date))
        if follow_df.empty:
            st.info("昨天没有可展示的次日结果。")
        else:
            st.dataframe(_prepare_followup_display(follow_df), width="stretch", hide_index=True)

    watchlist_df = load_watchlist_snapshot(base_dir=base_dir, trade_date=today_date)
    ready_df, trigger_df = split_actionable_watchlist(watchlist_df, top_n=30)
    st.markdown(f"**强势沉淀行动漏斗（{today_date}，近40日）**")
    ready_col, trigger_col = st.columns(2)
    with ready_col:
        st.markdown(f"**回踩到位·预备（{len(ready_df)}）**")
        if ready_df.empty:
            st.info("暂无回踩到位的预备标的。")
        else:
            st.dataframe(_prepare_watchlist_display(ready_df), width="stretch", hide_index=True)
            _render_watchlist_playbooks(ready_df)
    with trigger_col:
        st.markdown(f"**再启动·触发今日（{len(trigger_df)}）**")
        if trigger_df.empty:
            st.info("暂无再启动触发标的。")
        else:
            st.dataframe(_prepare_watchlist_display(trigger_df), width="stretch", hide_index=True)
            _render_watchlist_playbooks(trigger_df)

    with st.expander("查看全部沉淀名单", expanded=False):
        if watchlist_df.empty:
            st.info("近40日还没有可沉淀的强势观察名单。")
        else:
            states = sorted(str(value) for value in watchlist_df["state"].dropna().unique()) if "state" in watchlist_df.columns else []
            selected_states = st.multiselect("状态筛选", states, default=states)
            full_df = watchlist_df[watchlist_df["state"].isin(selected_states)] if selected_states else watchlist_df.iloc[0:0]
            st.dataframe(_prepare_watchlist_display(full_df), width="stretch", hide_index=True)
