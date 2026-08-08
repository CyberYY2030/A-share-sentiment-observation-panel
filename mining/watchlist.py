from __future__ import annotations

import json
import math
import sqlite3
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Iterable

import pandas as pd

from .capabilities import SCREENING_DEFINITION_VERSION
from .db import list_stock_trade_dates, now_str
from .features import board_kind, moving_average, volume_shrink_ratio
from .selection_context import SelectionContext, build_selection_context
from .trend_factors import TrendProfile, resolve_trend_profile

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

STATE_PULLBACK = "回踩中"
STATE_BROKEN = "破位失效"
STATE_RETRIGGER = "再启动"
STATE_READY = "回踩到位"
STATE_EXTEND = "延伸中"
WATCHLIST_STATES = frozenset(
    {
        STATE_PULLBACK,
        STATE_BROKEN,
        STATE_RETRIGGER,
        STATE_READY,
        STATE_EXTEND,
    }
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
        return STATE_PULLBACK
    if pullback < float(p["broken_pullback"]) or (below_ma60 and made_new_low_recent):
        return STATE_BROKEN

    in_ready_band = float(p["ready_hi"]) <= pullback <= float(p["ready_lo"])
    if in_ready_band and reclaim_ma10 and vol_expand_up and not made_new_low_recent:
        return STATE_RETRIGGER
    if (
        in_ready_band
        and math.isfinite(ma_proximity)
        and ma_proximity <= float(p["ma_tol"])
        and math.isfinite(shrink)
        and shrink <= float(p["shrink_max"])
        and not made_new_low_recent
    ):
        return STATE_READY
    if pullback < float(p["extend_max"]):
        return STATE_PULLBACK
    return STATE_EXTEND


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
    ready = sorted_df[sorted_df["state"] == STATE_READY].head(top_n).reset_index(drop=True)
    trigger = sorted_df[sorted_df["state"] == STATE_RETRIGGER].head(top_n).reset_index(drop=True)
    return ready, trigger


# V2 capability C uses explicit phase names.  The older constants above remain
# available for legacy reports while the formal selection path is migrated.
PULLBACK_STATE_EXTEND = "延伸中"
PULLBACK_STATE_PULLBACK = "回调中"
PULLBACK_STATE_READY = "回调到位"
PULLBACK_STATE_RETRIGGER = "再启动"
PULLBACK_STATE_BROKEN = "破位失效"
PULLBACK_SUPPORT_STATES = frozenset(
    {
        PULLBACK_STATE_EXTEND,
        PULLBACK_STATE_PULLBACK,
        PULLBACK_STATE_READY,
        PULLBACK_STATE_RETRIGGER,
        PULLBACK_STATE_BROKEN,
    }
)
PULLBACK_STATE_HISTORY_TABLE = "pullback_state_history"
@dataclass
class PullbackSupportEvaluation:
    """Single C-capability result shared by the phase view and second_launch."""

    rows: pd.DataFrame
    trend_profile: TrendProfile | None
    diagnostics: dict[str, Any]


def _v2_bool(value: Any) -> bool:
    return bool(value) if value is not None and not pd.isna(value) else False


def classify_pullback_support_state(
    row: pd.Series | Mapping[str, Any],
    *,
    prior_states: Sequence[str] = (),
) -> str:
    """Classify one formal C row; only a persisted pullback state may re-trigger."""
    pullback = _safe_float(row.get("pullback_pct"))
    close = _safe_float(row.get("adj_close"))
    ma10 = _safe_float(row.get("ma10"))
    ma20 = _safe_float(row.get("ma20"))
    ma_long = _safe_float(row.get("ma_long"))
    shrink = _safe_float(row.get("shrink_ratio"))
    below_long = math.isfinite(close) and math.isfinite(ma_long) and close < ma_long
    below_ma20 = math.isfinite(close) and math.isfinite(ma20) and close < ma20
    made_new_low = _v2_bool(row.get("made_new_low_recent"))
    reclaim = _v2_bool(row.get("reclaim_ma10"))
    activity_expand = _v2_bool(row.get("activity_expand"))
    support_near_ma10 = (
        math.isfinite(close)
        and math.isfinite(ma10)
        and ma10 > 0
        and abs(close / ma10 - 1.0) <= 0.03
    )
    prior_pullback = set(map(str, prior_states)).intersection(
        {PULLBACK_STATE_PULLBACK, PULLBACK_STATE_READY}
    )

    if not math.isfinite(pullback):
        return PULLBACK_STATE_PULLBACK
    if pullback < -0.30 or (below_long and (made_new_low or below_ma20)):
        return PULLBACK_STATE_BROKEN
    if pullback >= -0.05:
        return PULLBACK_STATE_EXTEND
    if reclaim and activity_expand:
        return PULLBACK_STATE_RETRIGGER if prior_pullback else PULLBACK_STATE_READY
    if support_near_ma10 and math.isfinite(shrink) and shrink <= 0.70 and not made_new_low:
        return PULLBACK_STATE_READY
    return PULLBACK_STATE_PULLBACK


def _normalize_a_qualified_dates(
    a_qualified_dates: Mapping[str, Iterable[str]] | None,
) -> dict[str, set[str]]:
    if not a_qualified_dates:
        return {}
    return {
        str(code).zfill(6): {str(trade_date) for trade_date in values}
        for code, values in a_qualified_dates.items()
    }


def _series_value(series: pd.Series, position: int) -> float:
    if position < 0 or position >= len(series):
        return math.nan
    return _safe_float(series.iloc[position])


def _evaluate_pullback_row(
    frame: pd.DataFrame,
    *,
    code: str,
    clean_dates: list[str],
    profile: TrendProfile,
    first_structure_date: str,
    a_dates: set[str],
    prior_states: Sequence[str],
    sec_name: str,
    strength_tier: str | None,
) -> dict[str, Any] | None:
    indexed = frame.assign(trade_date=frame["trade_date"].astype(str)).set_index("trade_date").reindex(clean_dates)
    close = pd.to_numeric(indexed.get("adj_close"), errors="coerce")
    if len(close) < profile.required_stock_bars or not close.tail(profile.required_stock_bars).notna().all():
        return None
    volume = pd.to_numeric(indexed.get("volume"), errors="coerce")
    adjusted_low = pd.to_numeric(indexed.get("adj_low", indexed.get("low")), errors="coerce")
    adjusted_open = pd.to_numeric(indexed.get("adj_open", indexed.get("open")), errors="coerce")
    ma10 = close.rolling(10, min_periods=10).mean()
    ma20 = close.rolling(20, min_periods=20).mean()
    ma_long = close.rolling(profile.long_window, min_periods=profile.long_window).mean()
    current_close = _series_value(close, len(close) - 1)
    current_ma10 = _series_value(ma10, len(ma10) - 1)
    current_ma20 = _series_value(ma20, len(ma20) - 1)
    current_ma_long = _series_value(ma_long, len(ma_long) - 1)
    if not (math.isfinite(current_close) and math.isfinite(current_ma10) and math.isfinite(current_ma_long)):
        return None

    structure_index = clean_dates.index(first_structure_date)
    window_close = close.iloc[structure_index:].dropna()
    if window_close.empty:
        return None
    peak_close = float(window_close.max())
    peak_date = str(window_close.idxmax())
    peak_position = clean_dates.index(peak_date)
    base_close = float(close.iloc[structure_index : peak_position + 1].min())
    base_date = str(close.iloc[structure_index : peak_position + 1].idxmin())
    peak_volume = volume.iloc[max(0, peak_position - 4) : peak_position + 1].dropna()
    recent_volume = volume.tail(5).dropna()
    shrink_ratio = (
        float(recent_volume.mean() / peak_volume.mean())
        if len(recent_volume) == 5 and len(peak_volume) == 5 and float(peak_volume.mean()) > 0
        else math.nan
    )
    changes = close.loc[peak_date:].pct_change(fill_method=None)
    pullback_negative_days = int(changes.lt(0).sum())
    recent_lows = adjusted_low.tail(3).dropna()
    made_new_low_recent = bool(
        len(recent_lows) == 3 and float(recent_lows.iloc[-1]) <= float(recent_lows.iloc[:-1].min())
    )
    previous_close = _series_value(close, len(close) - 2)
    previous_ma10 = _series_value(ma10, len(ma10) - 2)
    reclaim_ma10 = (
        math.isfinite(previous_close)
        and math.isfinite(previous_ma10)
        and previous_close < previous_ma10
        and current_close >= current_ma10
    )
    previous_volume = volume.iloc[-6:-1].dropna()
    current_open = _series_value(adjusted_open, len(adjusted_open) - 1)
    activity_expand = (
        math.isfinite(current_open)
        and current_close > current_open
        and len(previous_volume) == 5
        and math.isfinite(_series_value(volume, len(volume) - 1))
        and _series_value(volume, len(volume) - 1) > float(previous_volume.mean())
    )
    row: dict[str, Any] = {
        "sec_code": str(code).zfill(6),
        "sec_name": sec_name,
        "first_structure_date": first_structure_date,
        "a_qualified_date": min(a_dates) if a_dates else None,
        "a_qualified_once": bool(a_dates),
        "strength_tier": strength_tier,
        "main_rise_base": base_close,
        "main_rise_base_date": base_date,
        "peak_close": peak_close,
        "peak_date": peak_date,
        "run_up_pct": peak_close / base_close - 1.0 if base_close > 0 else math.nan,
        "adj_close": current_close,
        "reference_price": current_close,
        "pullback_pct": current_close / peak_close - 1.0 if peak_close > 0 else math.nan,
        "ma10": current_ma10,
        "ma20": current_ma20,
        "ma_long": current_ma_long,
        "dist_ma10": current_close / current_ma10 - 1.0 if current_ma10 > 0 else math.nan,
        "dist_ma20": current_close / current_ma20 - 1.0 if current_ma20 > 0 else math.nan,
        "dist_ma_long": current_close / current_ma_long - 1.0 if current_ma_long > 0 else math.nan,
        "shrink_ratio": shrink_ratio,
        "pullback_negative_days": pullback_negative_days,
        "made_new_low_recent": made_new_low_recent,
        "reclaim_ma10": reclaim_ma10,
        "activity_expand": activity_expand,
    }
    row["state"] = classify_pullback_support_state(row, prior_states=prior_states)
    return row


def evaluate_pullback_support(
    context: SelectionContext,
    *,
    a_qualified_dates: Mapping[str, Iterable[str]] | None = None,
    prior_state_history: Mapping[str, Sequence[str]] | None = None,
    a_strength_tiers: Mapping[str, str] | None = None,
) -> PullbackSupportEvaluation:
    """Build capability C from shared A structure history and close-final state history."""
    clean_dates = [str(date) for date in context.diagnostics.get("clean_dates", [])]
    profile = resolve_trend_profile(len(clean_dates))
    diagnostics: dict[str, Any] = {
        "trend_profile": profile.profile_id if profile else None,
        "universe_count": len(context.universe),
        "result_count": 0,
        "skipped_reason_counts": {},
    }
    if context.data_status != "ready" or profile is None or context.bars.empty:
        diagnostics["skipped_reason_counts"] = {
            "insufficient_history" if profile is None else "data_unavailable": len(context.universe)
        }
        return PullbackSupportEvaluation(pd.DataFrame(), profile, diagnostics)

    window_dates = clean_dates[-60:]
    a_pool = _normalize_a_qualified_dates(a_qualified_dates)
    prior_history = {str(code).zfill(6): tuple(states)[-5:] for code, states in (prior_state_history or {}).items()}
    tiers = {str(code).zfill(6): str(tier) for code, tier in (a_strength_tiers or {}).items()}
    universe_names = (
        context.universe.assign(sec_code=context.universe["sec_code"].astype(str).str.zfill(6))
        .set_index("sec_code")["sec_name"]
        .astype(str)
        .to_dict()
        if "sec_name" in context.universe.columns
        else {}
    )
    skipped = Counter()
    rows: list[dict[str, Any]] = []
    for code, frame in context.bars.groupby(context.bars["sec_code"].astype(str).str.zfill(6), sort=True):
        qualifying_a_dates = {date for date in a_pool.get(code, set()) if date in window_dates}
        if not qualifying_a_dates:
            skipped["never_in_a_qualified_pool"] += 1
            continue
        # C is defined by a prior formal A result, not by the legacy shared-MA
        # proxy.  The earliest qualifying A date is also the start of this C
        # structure window, keeping the displayed start inside the frozen
        # 60-session eligibility horizon.
        first_structure_date = min(qualifying_a_dates)
        row = _evaluate_pullback_row(
            frame,
            code=code,
            clean_dates=clean_dates,
            profile=profile,
            first_structure_date=first_structure_date,
            a_dates=qualifying_a_dates,
            prior_states=prior_history.get(code, ()),
            sec_name=universe_names.get(code, code),
            strength_tier=tiers.get(code),
        )
        if row is None:
            skipped["current_indicator_history_missing"] += 1
            continue
        rows.append(row)
    result = pd.DataFrame(rows)
    if not result.empty:
        state_order = {
            PULLBACK_STATE_RETRIGGER: 0,
            PULLBACK_STATE_READY: 1,
            PULLBACK_STATE_PULLBACK: 2,
            PULLBACK_STATE_EXTEND: 3,
            PULLBACK_STATE_BROKEN: 4,
        }
        result["state_sort"] = result["state"].map(state_order).fillna(99)
        result = result.sort_values(["state_sort", "pullback_pct", "sec_code"], ascending=[True, True, True]).drop(columns="state_sort").reset_index(drop=True)
    diagnostics["result_count"] = len(result)
    diagnostics["skipped_reason_counts"] = dict(sorted((key, value) for key, value in skipped.items() if value))
    return PullbackSupportEvaluation(result, profile, diagnostics)


def _load_a_qualified_pool(
    conn: sqlite3.Connection,
    clean_dates: Sequence[str],
) -> tuple[dict[str, set[str]], dict[str, str]]:
    if not clean_dates:
        return {}, {}
    marks = ",".join("?" for _ in clean_dates)
    try:
        rows = conn.execute(
            f"""
            SELECT c.sec_code, c.trade_date, c.features_json
            FROM candidates c
            JOIN strategy_runs r ON r.run_id=c.run_id
            JOIN selection_batches b ON b.batch_id=r.batch_id
            WHERE c.strategy_id='strong_trend' AND c.version=? AND c.sec_type='stock'
              AND c.trade_date IN ({marks}) AND r.mode='close_final'
              AND b.definition_version=? AND b.mode='close_final' AND b.status='complete'
            """,
            [SCREENING_DEFINITION_VERSION, *clean_dates, SCREENING_DEFINITION_VERSION],
        ).fetchall()
    except sqlite3.OperationalError:
        rows = conn.execute(
            f"""
            SELECT sec_code, trade_date, features_json
            FROM candidates
            WHERE strategy_id='strong_trend' AND version=? AND sec_type='stock' AND trade_date IN ({marks})
            """,
            [SCREENING_DEFINITION_VERSION, *clean_dates],
        ).fetchall()
    qualified: dict[str, set[str]] = {}
    tiers: dict[str, str] = {}
    for sec_code, trade_date, features_json in rows:
        code = str(sec_code).zfill(6)
        try:
            features = json.loads(features_json or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            features = {}
        tier = features.get("strength_tier")
        if tier in {"continuation", "fresh_breakout"}:
            qualified.setdefault(code, set()).add(str(trade_date))
            tiers[code] = str(tier)
    return qualified, tiers


def _ensure_pullback_state_history(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {PULLBACK_STATE_HISTORY_TABLE} (
          trade_date TEXT NOT NULL,
          sec_code TEXT NOT NULL,
          state TEXT NOT NULL,
          trend_profile TEXT,
          as_of TEXT,
          created_at TEXT,
          PRIMARY KEY (trade_date, sec_code)
        )
        """
    )
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS idx_{PULLBACK_STATE_HISTORY_TABLE}_code_date "
        f"ON {PULLBACK_STATE_HISTORY_TABLE}(sec_code, trade_date)"
    )


def load_prior_pullback_states(
    conn: sqlite3.Connection,
    trade_date: str,
    *,
    clean_dates: Sequence[str] | None = None,
) -> dict[str, tuple[str, ...]]:
    """Read only already-finalized C states from the five immediately prior sessions."""
    allowed_dates = set(map(str, clean_dates[-6:-1])) if clean_dates and len(clean_dates) >= 2 else None
    try:
        rows = conn.execute(
            f"""
            SELECT sec_code, trade_date, state
            FROM {PULLBACK_STATE_HISTORY_TABLE}
            WHERE trade_date < ?
            ORDER BY sec_code, trade_date DESC
            """,
            (str(trade_date),),
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    grouped: dict[str, list[str]] = {}
    for sec_code, state_date, state in rows:
        if allowed_dates is not None and str(state_date) not in allowed_dates:
            continue
        code = str(sec_code).zfill(6)
        if len(grouped.setdefault(code, [])) < 5:
            grouped[code].append(str(state))
    return {code: tuple(reversed(states)) for code, states in grouped.items()}


def build_pullback_support(
    conn: sqlite3.Connection,
    trade_date: str,
    *,
    context: SelectionContext | None = None,
) -> PullbackSupportEvaluation:
    """Build the formal C result; no raw price query is performed outside shared context."""
    resolved_context = context or build_selection_context(conn, trade_date, mode="close_final")
    clean_dates = [str(date) for date in resolved_context.diagnostics.get("clean_dates", [])]
    window_dates = [date for date in clean_dates if str(date) < str(trade_date)][-60:]
    a_qualified_dates, strength_tiers = _load_a_qualified_pool(conn, window_dates)
    prior_states = load_prior_pullback_states(conn, trade_date, clean_dates=clean_dates)
    return evaluate_pullback_support(
        resolved_context,
        a_qualified_dates=a_qualified_dates,
        prior_state_history=prior_states,
        a_strength_tiers=strength_tiers,
    )


def persist_pullback_support_states(
    conn: sqlite3.Connection,
    context: SelectionContext,
    evaluation: PullbackSupportEvaluation,
) -> int:
    """Persist only close-final C state; snapshot calls intentionally leave no history."""
    if context.mode != "close_final" or evaluation.rows.empty:
        return 0
    _ensure_pullback_state_history(conn)
    created_at = now_str()
    profile_id = evaluation.trend_profile.profile_id if evaluation.trend_profile else None
    for row in evaluation.rows.itertuples(index=False):
        conn.execute(
            f"""
            INSERT OR REPLACE INTO {PULLBACK_STATE_HISTORY_TABLE}
              (trade_date, sec_code, state, trend_profile, as_of, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (context.trade_date, str(row.sec_code).zfill(6), row.state, profile_id, context.as_of, created_at),
        )
    conn.commit()
    return len(evaluation.rows)
