from __future__ import annotations

import json
import math
import sqlite3
from typing import Any, Iterable

import pandas as pd

from .db import list_stock_trade_dates, now_str
from .features import board_kind, moving_average, volume_shrink_ratio

# Phase L: scanners that certify a stock as "prior strength". A stock only enters
# the funnel if it was flagged by one of these (true leader / relative-strength
# leader / momentum breakout / first launch). Shape/relay detectors (trend_embryo, second_launch)
# may appear as co-flags but never qualify a stock on their own.
STRENGTH_SCANNERS: tuple[str, ...] = (
    "rps_stock_top20",
    "true_leader",
    "momentum_breakout",
    "launch_burst",
)

WATCHLIST_DEFAULT_PARAMS: dict[str, Any] = {
    "vol_recent_n": 5,
    "vol_run_n": 5,
    "stop_n": 3,
    "red_day_thr": -5.0,
    "broken_pullback": -0.30,
    "ready_hi": -0.25,
    "ready_lo": -0.08,
    "extend_max": -0.05,
    "ma_tol": 0.03,
    "shrink_max": 0.7,
    "w_run": 1.0,
    "w_flag": 1.0,
    "w_shrink": 1.0,
    "w_support": 1.0,
    "w_trigger": 1.0,
    "w_dirty": 0.5,
    # Phase L strength gate (all tunable in one place).
    "min_runup": 0.30,
    "runup_base_n": 20,
    "strength_scanners": STRENGTH_SCANNERS,
    "w_limit": 0.15,
    "limit_up_main": 9.8,
    "limit_up_growth": 19.5,
}

WATCHLIST_COLUMNS = [
    "sec_code",
    "sec_name",
    "first_flag_date",
    "last_flag_date",
    "flag_count",
    "flag_strategies",
    "flag_strength_raw",
    "close",
    "first_flag_close",
    "peak_close_since_flag",
    "peak_date",
    "run_up_pct",
    "limit_up_count",
    "pullback_pct",
    "ma10",
    "ma20",
    "ma60",
    "ma_proximity",
    "below_ma60",
    "shrink_ratio",
    "pullback_red_days",
    "max_down_day",
    "made_new_low_recent",
    "reclaim_ma10",
    "vol_expand_up",
    "state",
    "triage",
    "days_since_flag",
    "days_since_peak",
]


def _params(params: dict[str, Any] | None) -> dict[str, Any]:
    return {**WATCHLIST_DEFAULT_PARAMS, **(params or {})}


def _empty_watchlist() -> pd.DataFrame:
    return pd.DataFrame(columns=WATCHLIST_COLUMNS)


def _safe_float(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return math.nan
    return result if math.isfinite(result) else math.nan


def _norm(value: float, cap: float) -> float:
    if not math.isfinite(value):
        return 0.0
    return max(0.0, min(float(value) / cap, 1.0))


def _limit_up_threshold(code: str, params: dict[str, Any]) -> float:
    """Daily change_pct threshold that counts as a limit-up for the code's board."""
    code = str(code).zfill(6)
    if board_kind(code) in {"gem", "star"} or code.startswith(("8", "4", "92")):
        return float(params["limit_up_growth"])
    return float(params["limit_up_main"])


def _load_flagged_candidates(
    conn: sqlite3.Connection,
    dates: list[str],
    source_strategies: tuple[str, ...],
) -> pd.DataFrame:
    if not dates or not source_strategies:
        return pd.DataFrame()
    date_ph = ",".join("?" for _ in dates)
    strategy_ph = ",".join("?" for _ in source_strategies)
    return pd.read_sql_query(
        f"""
        SELECT trade_date, strategy_id, sec_code, COALESCE(sec_name, sec_code) AS sec_name, features_json
        FROM candidates
        WHERE sec_type='stock'
          AND trade_date IN ({date_ph})
          AND strategy_id IN ({strategy_ph})
        """,
        conn,
        params=[*dates, *source_strategies],
    )


def _feature_strength(value: Any) -> float:
    try:
        features = json.loads(value or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return math.nan
    candidates = []
    for key in ("rps_N", "ret_5d"):
        score = _safe_float(features.get(key))
        if math.isfinite(score):
            candidates.append(score)
    return max(candidates) if candidates else math.nan


def _load_stock_prices(
    conn: sqlite3.Connection,
    sec_codes: list[str],
    start_date: str,
    end_date: str,
) -> pd.DataFrame:
    if not sec_codes:
        return pd.DataFrame()
    code_ph = ",".join("?" for _ in sec_codes)
    return pd.read_sql_query(
        f"""
        SELECT sec_code, trade_date, open, low, close, change_pct, volume
        FROM ash.kline_daily
        WHERE sec_type='stock'
          AND sec_code IN ({code_ph})
          AND trade_date BETWEEN ? AND ?
          AND close > 0
        """,
        conn,
        params=[*sec_codes, start_date, end_date],
    )


def _calendar_gap(calendar_index: dict[str, int], start: str, end: str) -> int | None:
    start_idx = calendar_index.get(start)
    end_idx = calendar_index.get(end)
    if start_idx is None or end_idx is None:
        return None
    return int(end_idx - start_idx)


def classify_state(row: pd.Series | dict[str, Any], params: dict[str, Any] | None = None) -> str:
    p = _params(params)
    pullback = _safe_float(row.get("pullback_pct"))
    ma_proximity = _safe_float(row.get("ma_proximity"))
    shrink = _safe_float(row.get("shrink_ratio"))
    below_ma60 = bool(row.get("below_ma60"))
    made_new_low_recent = bool(row.get("made_new_low_recent"))
    reclaim_ma10 = bool(row.get("reclaim_ma10"))
    vol_expand_up = bool(row.get("vol_expand_up"))

    if not math.isfinite(pullback):
        return "回踩中"
    if pullback < float(p["broken_pullback"]) or (below_ma60 and made_new_low_recent):
        return "破位失效"

    in_ready_band = float(p["ready_hi"]) <= pullback <= float(p["ready_lo"])
    if in_ready_band and reclaim_ma10 and vol_expand_up and not made_new_low_recent:
        return "再启动"
    if (
        in_ready_band
        and math.isfinite(ma_proximity)
        and ma_proximity <= float(p["ma_tol"])
        and math.isfinite(shrink)
        and shrink <= float(p["shrink_max"])
        and not made_new_low_recent
    ):
        return "回踩到位"
    if pullback < float(p["extend_max"]):
        return "回踩中"
    return "延伸中"


def _triage(row: dict[str, Any], params: dict[str, Any]) -> float:
    run_up = _safe_float(row.get("run_up_pct"))
    flag_count = _safe_float(row.get("flag_count"))
    shrink = _safe_float(row.get("shrink_ratio"))
    ma_proximity = _safe_float(row.get("ma_proximity"))
    red_days = _safe_float(row.get("pullback_red_days"))
    limit_up_count = _safe_float(row.get("limit_up_count"))
    trigger = bool(row.get("reclaim_ma10")) and bool(row.get("vol_expand_up"))
    shrink_score = max(0.0, 1.0 - shrink) if math.isfinite(shrink) else 0.0
    support_score = (
        max(0.0, 1.0 - ma_proximity / max(float(params["ma_tol"]), 1e-9))
        if math.isfinite(ma_proximity)
        else 0.0
    )
    return (
        float(params["w_run"]) * _norm(run_up, 0.8)
        + float(params["w_flag"]) * _norm(flag_count, 10.0)
        + float(params["w_shrink"]) * min(shrink_score, 1.0)
        + float(params["w_support"]) * min(support_score, 1.0)
        + float(params["w_trigger"]) * float(trigger)
        + float(params["w_limit"]) * _norm(limit_up_count, 5.0)
        - float(params["w_dirty"]) * max(red_days if math.isfinite(red_days) else 0.0, 0.0)
    )


def build_watchlist(
    conn: sqlite3.Connection,
    trade_date: str,
    lookback: int = 40,
    source_strategies: Iterable[str] = (
        "trend_embryo",
        "second_launch",
        "true_leader",
        "rps_stock_top20",
        "momentum_breakout",
        "launch_burst",
    ),
    params: dict[str, Any] | None = None,
) -> pd.DataFrame:
    """Build the watchlist funnel for human review.

    The returned `state` field can be used as a future evaluation cohort, but this
    function is a triage layer only and does not claim predictive edge.
    """
    p = _params(params)
    source_tuple = tuple(source_strategies)
    if lookback <= 0 or not source_tuple:
        return _empty_watchlist()

    flag_dates = list_stock_trade_dates(conn, end_date=trade_date, limit=lookback, include_end=True)
    if not flag_dates:
        return _empty_watchlist()
    flags = _load_flagged_candidates(conn, flag_dates, source_tuple)
    if flags.empty:
        return _empty_watchlist()

    flags["sec_code"] = flags["sec_code"].astype(str).str.zfill(6)
    flags["trade_date"] = flags["trade_date"].astype(str)
    flags["flag_strength_raw"] = flags["features_json"].map(_feature_strength)
    grouped = (
        flags.groupby("sec_code", as_index=False)
        .agg(
            sec_name=("sec_name", "last"),
            first_flag_date=("trade_date", "min"),
            last_flag_date=("trade_date", "max"),
            flag_count=("trade_date", "size"),
            flag_strategies=("strategy_id", lambda values: ",".join(sorted(set(map(str, values))))),
            flag_strength_raw=("flag_strength_raw", "max"),
        )
        .copy()
    )

    # L1 strength gate (part 1): a stock must have been certified by a strength
    # scanner. Shape/relay flags alone never qualify it. The run-up threshold is
    # applied below once we have priced the peak.
    strength_set = {str(name) for name in p["strength_scanners"]}
    grouped = grouped[
        grouped["flag_strategies"].map(
            lambda value: bool(strength_set & {token for token in str(value).split(",") if token})
        )
    ].copy()
    if grouped.empty:
        return _empty_watchlist()

    all_dates = list_stock_trade_dates(conn, end_date=trade_date, limit=max(lookback + 65, 100), include_end=True)
    if not all_dates or trade_date not in all_dates:
        return _empty_watchlist()
    prices = _load_stock_prices(conn, grouped["sec_code"].tolist(), all_dates[0], trade_date)
    if prices.empty:
        return _empty_watchlist()

    prices["sec_code"] = prices["sec_code"].astype(str).str.zfill(6)
    close_pivot = prices.pivot_table(index="trade_date", columns="sec_code", values="close", aggfunc="last").reindex(all_dates).sort_index()
    open_pivot = prices.pivot_table(index="trade_date", columns="sec_code", values="open", aggfunc="last").reindex(all_dates).sort_index()
    low_pivot = prices.pivot_table(index="trade_date", columns="sec_code", values="low", aggfunc="last").reindex(all_dates).sort_index()
    volume_pivot = prices.pivot_table(index="trade_date", columns="sec_code", values="volume", aggfunc="last").reindex(all_dates).sort_index()
    change_pivot = prices.pivot_table(index="trade_date", columns="sec_code", values="change_pct", aggfunc="last").reindex(all_dates).sort_index()
    if trade_date not in close_pivot.index:
        return _empty_watchlist()
    ma10 = close_pivot.apply(lambda series: moving_average(series, 10))
    ma20 = close_pivot.apply(lambda series: moving_average(series, 20))
    ma60 = close_pivot.apply(lambda series: moving_average(series, 60))
    calendar_index = {day: idx for idx, day in enumerate(all_dates)}

    rows: list[dict[str, object]] = []
    for item in grouped.itertuples(index=False):
        code = str(item.sec_code).zfill(6)
        if code not in close_pivot.columns:
            continue
        today_close = _safe_float(close_pivot.at[trade_date, code])
        if not math.isfinite(today_close) or today_close <= 0:
            continue

        first_flag_date = str(item.first_flag_date)
        last_flag_date = str(item.last_flag_date)
        first_flag_close = _safe_float(close_pivot.at[first_flag_date, code]) if first_flag_date in close_pivot.index else math.nan
        series_since_flag = close_pivot.loc[first_flag_date:trade_date, code].dropna()
        if series_since_flag.empty:
            continue
        peak_close = float(series_since_flag.max())
        if not math.isfinite(peak_close) or peak_close <= 0:
            continue
        peak_date = str(series_since_flag.idxmax())

        # L2: anchor the run-up to the real launch base = lowest close within the
        # `runup_base_n` bars up to the peak (the recent low before the surge),
        # not the (possibly late) first-flag close. Fall back to first_flag_close.
        peak_idx = calendar_index.get(peak_date)
        runup_base_n = int(p["runup_base_n"])
        base_close = math.nan
        if peak_idx is not None:
            base_start_date = all_dates[max(0, peak_idx - runup_base_n)]
            base_window = close_pivot.loc[base_start_date:peak_date, code].dropna()
            if not base_window.empty:
                base_close = float(base_window.min())
        if not (math.isfinite(base_close) and base_close > 0):
            base_close = first_flag_close
        run_up_pct = (
            peak_close / base_close - 1.0
            if math.isfinite(base_close) and base_close > 0
            else math.nan
        )

        # L1 strength gate (part 2): require a clear prior main-up advance.
        if not (math.isfinite(run_up_pct) and run_up_pct >= float(p["min_runup"])):
            continue

        ma10_value = _safe_float(ma10.at[trade_date, code]) if code in ma10.columns else math.nan
        ma20_value = _safe_float(ma20.at[trade_date, code]) if code in ma20.columns else math.nan
        ma60_value = _safe_float(ma60.at[trade_date, code]) if code in ma60.columns else math.nan
        ma_distances = [abs(today_close / value - 1.0) for value in (ma10_value, ma20_value) if math.isfinite(value) and value > 0]
        ma_proximity = min(ma_distances) if ma_distances else math.nan

        first_idx = calendar_index.get(first_flag_date)
        vol_run_n = int(p["vol_run_n"])
        vol_recent_n = int(p["vol_recent_n"])
        if first_idx is None:
            run_volume = pd.Series(dtype="float64")
        else:
            run_start = max(0, first_idx - vol_run_n + 1)
            run_volume = volume_pivot.loc[all_dates[run_start : first_idx + 1], code].dropna()
        recent_volume = volume_pivot[code].dropna().tail(vol_recent_n)
        shrink = volume_shrink_ratio(recent_volume, run_volume)

        peak_to_today_change = pd.to_numeric(change_pivot.loc[peak_date:trade_date, code].dropna(), errors="coerce")
        pullback_red_days = int((peak_to_today_change < float(p["red_day_thr"])).sum()) if not peak_to_today_change.empty else 0
        max_down_day = float(peak_to_today_change.min()) if not peak_to_today_change.empty else math.nan

        stop_n = int(p["stop_n"])
        recent_lows = pd.to_numeric(low_pivot[code].dropna().tail(stop_n), errors="coerce")
        made_new_low_recent = False
        if len(recent_lows) >= stop_n and len(recent_lows.iloc[:-1]) > 0:
            made_new_low_recent = bool(float(recent_lows.iloc[-1]) <= float(recent_lows.iloc[:-1].min()))

        today_open = _safe_float(open_pivot.at[trade_date, code]) if code in open_pivot.columns else math.nan
        today_volume = _safe_float(volume_pivot.at[trade_date, code]) if code in volume_pivot.columns else math.nan
        previous_recent_volume = volume_pivot[code].dropna().iloc[-(vol_recent_n + 1) : -1]
        previous_volume_avg = _safe_float(previous_recent_volume.mean()) if len(previous_recent_volume) >= vol_recent_n else math.nan
        reclaim_ma10 = math.isfinite(ma10_value) and today_close >= ma10_value
        vol_expand_up = (
            math.isfinite(today_open)
            and today_close > today_open
            and math.isfinite(today_volume)
            and math.isfinite(previous_volume_avg)
            and today_volume > previous_volume_avg
        )
        below_ma60 = math.isfinite(ma60_value) and today_close < ma60_value

        # L3: count limit-up days over the lookback window (soft triage bonus only,
        # no hard gate). Approximated from change_pct against the board threshold.
        limit_up_thr = _limit_up_threshold(code, p)
        window_changes = pd.to_numeric(
            change_pivot.loc[flag_dates[0]:trade_date, code].dropna(), errors="coerce"
        )
        limit_up_count = int((window_changes >= limit_up_thr).sum()) if not window_changes.empty else 0
        row = {
            "sec_code": code,
            "sec_name": item.sec_name,
            "first_flag_date": first_flag_date,
            "last_flag_date": last_flag_date,
            "flag_count": int(item.flag_count),
            "flag_strategies": item.flag_strategies,
            "flag_strength_raw": _safe_float(item.flag_strength_raw),
            "close": today_close,
            "first_flag_close": first_flag_close,
            "peak_close_since_flag": peak_close,
            "peak_date": peak_date,
            "run_up_pct": run_up_pct,
            "limit_up_count": limit_up_count,
            "pullback_pct": today_close / peak_close - 1.0,
            "ma10": ma10_value,
            "ma20": ma20_value,
            "ma60": ma60_value,
            "ma_proximity": ma_proximity,
            "below_ma60": below_ma60,
            "shrink_ratio": shrink,
            "pullback_red_days": pullback_red_days,
            "max_down_day": max_down_day,
            "made_new_low_recent": made_new_low_recent,
            "reclaim_ma10": reclaim_ma10,
            "vol_expand_up": vol_expand_up,
            "days_since_flag": _calendar_gap(calendar_index, last_flag_date, trade_date),
            "days_since_peak": _calendar_gap(calendar_index, peak_date, trade_date),
        }
        row["state"] = classify_state(row, p)
        row["triage"] = _triage(row, p)
        rows.append(row)

    if not rows:
        return _empty_watchlist()
    result = pd.DataFrame(rows)
    return result.sort_values(["triage", "last_flag_date", "pullback_pct"], ascending=[False, False, True]).reset_index(drop=True)


SNAPSHOT_COLUMNS = [
    "snapshot_date",
    "sec_code",
    "sec_name",
    "state",
    "triage",
    "entry_price",
    "run_up_pct",
    "shrink_ratio",
    "ma_proximity",
    "pullback_pct",
    "reclaim_ma10",
    "vol_expand_up",
    "flag_count",
    "days_since_flag",
    "flag_strategies",
    "created_at",
]


def _snapshot_value(row: pd.Series, column: str) -> Any:
    value = row.get(column)
    if pd.isna(value):
        return None
    return value


def persist_watchlist_snapshot(
    conn: sqlite3.Connection,
    trade_date: str,
    params: dict[str, Any] | None = None,
) -> int:
    """Persist the full watchlist funnel for later forward validation."""
    watchlist = build_watchlist(conn, trade_date, params=params)
    if watchlist.empty:
        conn.commit()
        return 0

    written = 0
    created_at = now_str()
    for _, row in watchlist.iterrows():
        entry_price = _safe_float(row.get("close"))
        if not math.isfinite(entry_price) or entry_price <= 0:
            continue
        values = {
            "snapshot_date": str(trade_date),
            "sec_code": str(row.get("sec_code") or "").zfill(6),
            "sec_name": _snapshot_value(row, "sec_name"),
            "state": str(row.get("state") or ""),
            "triage": _snapshot_value(row, "triage"),
            "entry_price": entry_price,
            "run_up_pct": _snapshot_value(row, "run_up_pct"),
            "shrink_ratio": _snapshot_value(row, "shrink_ratio"),
            "ma_proximity": _snapshot_value(row, "ma_proximity"),
            "pullback_pct": _snapshot_value(row, "pullback_pct"),
            "reclaim_ma10": int(bool(row.get("reclaim_ma10"))),
            "vol_expand_up": int(bool(row.get("vol_expand_up"))),
            "flag_count": _snapshot_value(row, "flag_count"),
            "days_since_flag": _snapshot_value(row, "days_since_flag"),
            "flag_strategies": _snapshot_value(row, "flag_strategies"),
            "created_at": created_at,
        }
        conn.execute(
            """
            INSERT OR REPLACE INTO watchlist_snapshots (
              snapshot_date, sec_code, sec_name, state, triage, entry_price,
              run_up_pct, shrink_ratio, ma_proximity, pullback_pct,
              reclaim_ma10, vol_expand_up, flag_count, days_since_flag,
              flag_strategies, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            tuple(values[column] for column in SNAPSHOT_COLUMNS),
        )
        written += 1
    conn.commit()
    return written


def split_actionable_watchlist(
    watchlist: pd.DataFrame,
    top_n: int = 30,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    if watchlist.empty:
        return watchlist.copy(), watchlist.copy()
    sorted_df = watchlist.sort_values("triage", ascending=False).reset_index(drop=True)
    ready = sorted_df[sorted_df["state"] == "回踩到位"].head(top_n).reset_index(drop=True)
    trigger = sorted_df[sorted_df["state"] == "再启动"].head(top_n).reset_index(drop=True)
    return ready, trigger
