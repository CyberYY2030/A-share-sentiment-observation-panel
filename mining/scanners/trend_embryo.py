from __future__ import annotations

import sqlite3
from typing import Any

import pandas as pd

from ..db import list_stock_trade_dates
from ..features import is_limit_up
from ..selection_context import SelectionContext
from ..universe import build_universe
from . import Candidate, Scanner, register


def build_v2_path_context(context: SelectionContext) -> pd.DataFrame:
    """Expose the legacy embryo path evidence as B explanation, never as a ranking gate."""
    columns = [
        "sec_code",
        "limit_up_count_5d",
        "small_yang_count",
        "single_day_max_change",
        "path_context",
    ]
    if context.bars.empty or context.universe.empty:
        return pd.DataFrame(columns=columns)
    board_by_code = (
        context.universe.assign(sec_code=context.universe["sec_code"].astype(str).str.zfill(6))
        .set_index("sec_code")["board"]
        .to_dict()
        if "board" in context.universe.columns
        else {}
    )
    rows: list[dict[str, object]] = []
    for code, frame in context.bars.groupby(context.bars["sec_code"].astype(str).str.zfill(6), sort=True):
        recent = frame.sort_values("trade_date", kind="stable").tail(5).copy()
        if len(recent) < 5:
            continue
        changes = pd.to_numeric(recent.get("change_pct"), errors="coerce")
        board = board_by_code.get(code)
        limit_up_count = (
            int(sum(is_limit_up(row.close, row.pre_close, board) for row in recent[["close", "pre_close"]].itertuples(index=False)))
            if board and {"close", "pre_close"}.issubset(recent.columns)
            else 0
        )
        small_yang = int((changes.gt(0) & changes.lt(5.0)).sum())
        max_change = float(changes.max()) if changes.notna().any() else float("nan")
        if limit_up_count >= 2:
            label = "已过度延伸"
        elif limit_up_count >= 1 or (pd.notna(max_change) and max_change >= 8.0):
            label = "加速"
        elif small_yang >= 3:
            label = "蓄势"
        else:
            label = "常态"
        rows.append(
            {
                "sec_code": code,
                "limit_up_count_5d": limit_up_count,
                "small_yang_count": small_yang,
                "single_day_max_change": max_change,
                "path_context": label,
            }
        )
    return pd.DataFrame(rows, columns=columns)


def _load_stock_history(conn: sqlite3.Connection, sec_codes: list[str], dates: list[str]) -> pd.DataFrame:
    if not sec_codes or not dates:
        return pd.DataFrame()
    code_ph = ",".join("?" for _ in sec_codes)
    date_ph = ",".join("?" for _ in dates)
    return pd.read_sql_query(
        f"""
        SELECT sec_code, trade_date, close, pre_close, change_pct
        FROM ash.kline_daily
        WHERE sec_type='stock'
          AND sec_code IN ({code_ph})
          AND trade_date IN ({date_ph})
        """,
        conn,
        params=[*sec_codes, *dates],
    )


def select_candidates_from_universe(
    conn: sqlite3.Connection,
    trade_date: str,
    universe: pd.DataFrame,
    params: dict[str, Any],
    strategy_id: str,
    version: str,
) -> list[Candidate]:
    if universe.empty:
        return []

    dates = list_stock_trade_dates(conn, end_date=trade_date, limit=6, include_end=True)
    if len(dates) < 6:
        return []

    sec_codes = universe["sec_code"].astype(str).tolist()
    history = _load_stock_history(conn, sec_codes, dates)
    if history.empty:
        return []

    board_by_code = universe.set_index("sec_code")["board"].to_dict()
    history["board"] = history["sec_code"].map(board_by_code)
    history["change_pct"] = pd.to_numeric(history["change_pct"], errors="coerce")
    history["is_limit_up"] = history.apply(
        lambda row: is_limit_up(row["close"], row["pre_close"], row["board"]), axis=1
    )

    close_pivot = (
        history.pivot_table(index="trade_date", columns="sec_code", values="close", aggfunc="last")
        .reindex(dates)
        .sort_index()
    )
    if close_pivot.empty or trade_date not in close_pivot.index:
        return []

    ret_5d = ((close_pivot.iloc[-1] / close_pivot.iloc[0]) - 1.0) * 100.0
    ret_5d.name = "ret_5d"
    grouped = history.groupby("sec_code", as_index=True)
    limit_up_count = grouped["is_limit_up"].sum().rename("limit_up_count_5d")
    small_yang_count = (
        history[
            (history["change_pct"] > 0)
            & (history["change_pct"] < float(params["small_body_max"]))
        ]
        .groupby("sec_code")["change_pct"]
        .count()
        .rename("small_yang_count")
    )
    single_day_max_change = grouped["change_pct"].max().rename("single_day_max_change")

    result = universe[["sec_code", "sec_name", "close", "amount", "board"]].copy()
    result["amount"] = pd.to_numeric(result["amount"], errors="coerce")
    result["ret_5d"] = result["sec_code"].map(ret_5d)
    result["limit_up_count_5d"] = result["sec_code"].map(limit_up_count).fillna(0).astype(int)
    result["small_yang_count"] = result["sec_code"].map(small_yang_count).fillna(0).astype(int)
    result["single_day_max_change"] = result["sec_code"].map(single_day_max_change)

    selected = result[
        (pd.to_numeric(result["ret_5d"], errors="coerce") > float(params["embryo_ret_lo"]))
        & (pd.to_numeric(result["ret_5d"], errors="coerce") < float(params["embryo_ret_hi"]))
        & (result["limit_up_count_5d"] <= 1)
        & (result["amount"] >= float(params["amount_min"]))
    ].copy()
    if selected.empty:
        return []

    selected["path"] = selected["limit_up_count_5d"].map(lambda count: "no_limit" if int(count) == 0 else "one_limit")
    selected["score"] = 0.0
    selected["score_mode"] = "candidate_pool"
    selected["independence"] = None
    selected = selected.sort_values(
        by=["sec_code"], ascending=[True]
    ).head(int(params["top_n"]))

    candidates: list[Candidate] = []
    for rank, row in enumerate(selected.itertuples(index=False), start=1):
        candidates.append(
            Candidate(
                strategy_id=strategy_id,
                version=version,
                trade_date=trade_date,
                sec_type="stock",
                sec_code=row.sec_code,
                sec_name=row.sec_name,
                entry_price=float(row.close),
                features={
                    "ret_5d": float(row.ret_5d),
                    "limit_up_count_5d": int(row.limit_up_count_5d),
                    "small_yang_count": int(row.small_yang_count),
                    "single_day_max_change": float(row.single_day_max_change),
                    "amount": float(row.amount),
                    "board": row.board,
                    "path": row.path,
                    "score": float(row.score),
                    "score_mode": row.score_mode,
                    "independence": row.independence,
                },
                rank=rank,
            )
        )
    return candidates


@register
class TrendEmbryoScanner(Scanner):
    strategy_id = "trend_embryo"
    version = "v1.1"
    kind = "stock"
    description = "Early trend embryo candidate-pool scanner for post-close discovery before acceleration."
    default_params = {
        "embryo_ret_lo": 13.0,
        "embryo_ret_hi": 40.0,
        "small_body_max": 5.0,
        "amount_min": 500_000_000,
        "spike_ref": 9.5,
        "top_n": 30,
        "w_path": 2.0,
        "w_yang": 1.0,
        "w_spike": 0.3,
        "w_indep": 1.0,
    }

    def run(self, conn: sqlite3.Connection, trade_date: str) -> list[Candidate]:
        universe = build_universe(conn, trade_date)
        self.last_universe_size = len(universe)
        return select_candidates_from_universe(
            conn=conn,
            trade_date=trade_date,
            universe=universe,
            params=self.params,
            strategy_id=self.strategy_id,
            version=self.version,
        )
