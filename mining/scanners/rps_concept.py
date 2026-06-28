from __future__ import annotations

import sqlite3

import pandas as pd

from ..db import list_concept_trade_dates
from ..features import ret_n, rps
from . import Candidate, Scanner, register


@register
class RpsConceptScanner(Scanner):
    strategy_id = "rps_concept_top20"
    version = "v1.0"
    kind = "concept"
    description = "Top concept relative strength ranking."
    default_params = {
        "lookback_short": 10,
        "lookback_long": 30,
        "threshold": 80.0,
        "top_n": 20,
    }

    def run(self, conn: sqlite3.Connection, trade_date: str) -> list[Candidate]:
        dates = list_concept_trade_dates(
            conn,
            end_date=trade_date,
            limit=max(self.params["lookback_short"], self.params["lookback_long"]) + 1,
            include_end=True,
        )
        if len(dates) < max(self.params["lookback_short"], self.params["lookback_long"]) + 1:
            self.last_universe_size = 0
            return []

        history = pd.read_sql_query(
            f"""
            SELECT
              k.concept_code,
              k.trade_date,
              k.close,
              m.name
            FROM ths.concept_kline k
            LEFT JOIN ths.concept_master m
              ON m.index_code = k.concept_code
            WHERE k.trade_date IN ({",".join("?" for _ in dates)})
            """,
            conn,
            params=dates,
        )
        if history.empty:
            self.last_universe_size = 0
            return []

        pivot = history.pivot_table(
            index="trade_date", columns="concept_code", values="close", aggfunc="last"
        ).sort_index()
        ret_short = pivot.apply(lambda series: ret_n(series, self.params["lookback_short"]))
        ret_long = pivot.apply(lambda series: ret_n(series, self.params["lookback_long"]))

        latest = history[history["trade_date"] == trade_date][
            ["concept_code", "name", "close"]
        ].drop_duplicates("concept_code")
        latest["name"] = latest["name"].fillna(latest["concept_code"])
        latest["ret_10"] = latest["concept_code"].map(ret_short)
        latest["ret_30"] = latest["concept_code"].map(ret_long)
        latest["rps_10"] = latest["concept_code"].map(rps(ret_short))
        latest["rps_30"] = latest["concept_code"].map(rps(ret_long))
        latest["score"] = (latest["rps_10"] + latest["rps_30"]) / 2.0
        latest = latest[
            (latest["rps_10"] >= self.params["threshold"])
            & (latest["rps_30"] >= self.params["threshold"])
        ].copy()
        self.last_universe_size = pivot.shape[1]
        if latest.empty:
            return []
        latest = latest.sort_values(
            by=["score", "rps_30", "concept_code"], ascending=[False, False, True]
        ).head(int(self.params["top_n"]))

        candidates: list[Candidate] = []
        for rank, row in enumerate(latest.itertuples(index=False), start=1):
            candidates.append(
                Candidate(
                    strategy_id=self.strategy_id,
                    version=self.version,
                    trade_date=trade_date,
                    sec_type="concept",
                    sec_code=row.concept_code,
                    sec_name=row.name,
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

