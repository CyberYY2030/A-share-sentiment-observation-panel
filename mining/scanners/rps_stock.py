from __future__ import annotations

import sqlite3

import pandas as pd

from ..db import list_stock_trade_dates
from ..features import ret_n, rps
from ..universe import build_universe
from . import Candidate, Scanner, register


def select_candidates_from_universe(
    conn: sqlite3.Connection,
    trade_date: str,
    universe: pd.DataFrame,
    params: dict[str, float],
    strategy_id: str,
    version: str,
) -> list[Candidate]:
    if universe.empty:
        return []

    lookback = max(params["lookback_short"], params["lookback_long"])
    history_dates = list_stock_trade_dates(
        conn, end_date=trade_date, limit=lookback, include_end=False
    )
    if len(history_dates) < lookback:
        return []

    code_ph = ",".join("?" for _ in universe["sec_code"])
    date_ph = ",".join("?" for _ in history_dates)
    history = pd.read_sql_query(
        f"""
        SELECT sec_code, trade_date, close
        FROM ash.kline_daily
        WHERE sec_type='stock'
          AND sec_code IN ({code_ph})
          AND trade_date IN ({date_ph})
        """,
        conn,
        params=[*universe["sec_code"].tolist(), *history_dates],
    )
    if history.empty:
        return []

    current = universe[["sec_code", "close"]].copy()
    current["trade_date"] = trade_date
    history = pd.concat([history, current], ignore_index=True)
    pivot = history.pivot_table(
        index="trade_date", columns="sec_code", values="close", aggfunc="last"
    ).sort_index()
    ret_short = pivot.apply(lambda series: ret_n(series, params["lookback_short"]))
    ret_long = pivot.apply(lambda series: ret_n(series, params["lookback_long"]))

    result = universe[["sec_code", "sec_name", "close"]].copy()
    result["ret_10"] = result["sec_code"].map(ret_short)
    result["ret_30"] = result["sec_code"].map(ret_long)
    result["rps_10"] = result["sec_code"].map(rps(ret_short))
    result["rps_30"] = result["sec_code"].map(rps(ret_long))
    result["score"] = (result["rps_10"] + result["rps_30"]) / 2.0
    result = result[
        (result["rps_10"] >= params["threshold"])
        & (result["rps_30"] >= params["threshold"])
    ].copy()
    if result.empty:
        return []
    result = result.sort_values(
        by=["score", "rps_30", "sec_code"], ascending=[False, False, True]
    ).head(int(params["top_n"]))

    candidates: list[Candidate] = []
    for rank, row in enumerate(result.itertuples(index=False), start=1):
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
                    "ret_10": float(row.ret_10),
                    "ret_30": float(row.ret_30),
                    "rps_10": float(row.rps_10),
                    "rps_30": float(row.rps_30),
                    "score": float(row.score),
                },
                rank=rank,
            )
        )
    return candidates


@register
class RpsStockScanner(Scanner):
    strategy_id = "rps_stock_top20"
    version = "v1.0"
    kind = "stock"
    description = "Top A-share relative strength ranking."
    default_params = {
        "lookback_short": 10,
        "lookback_long": 30,
        "threshold": 80.0,
        "top_n": 20,
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
