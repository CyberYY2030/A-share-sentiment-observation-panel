from __future__ import annotations

import math
import sqlite3

from ..capabilities import SCREENING_DEFINITION_VERSION
from ..selection_context import build_selection_context
from ..watchlist import (
    PULLBACK_STATE_RETRIGGER,
    PullbackSupportEvaluation,
    build_pullback_support,
    persist_pullback_support_states,
)
from . import Candidate, Scanner, register


def _ma_proximity(row: object) -> float | None:
    values: list[float] = []
    for field in ("dist_ma10", "dist_ma20"):
        try:
            value = float(getattr(row, field))
        except (TypeError, ValueError):
            continue
        if math.isfinite(value):
            values.append(abs(value))
    return min(values) if values else None


def select_candidates_from_pullback_support(
    evaluation: PullbackSupportEvaluation,
    *,
    trade_date: str,
    strategy_id: str,
    version: str,
    top_n: int,
) -> list[Candidate]:
    """Expose only the shared C rows that have a confirmed re-start phase."""
    rows = evaluation.rows
    if rows.empty:
        return []
    selected = rows[rows["state"].eq(PULLBACK_STATE_RETRIGGER)].head(int(top_n))
    candidates: list[Candidate] = []
    for rank, row in enumerate(selected.itertuples(index=False), start=1):
        candidates.append(
            Candidate(
                strategy_id=strategy_id,
                version=version,
                trade_date=str(trade_date),
                sec_type="stock",
                sec_code=str(row.sec_code).zfill(6),
                sec_name=str(row.sec_name),
                entry_price=float(row.reference_price),
                features={
                    "reference_price": float(row.reference_price),
                    "state": row.state,
                    "strength_tier": row.strength_tier,
                    "first_structure_date": row.first_structure_date,
                    "a_qualified_date": row.a_qualified_date,
                    "main_rise_base": float(row.main_rise_base),
                    "main_rise_base_date": row.main_rise_base_date,
                    "peak_close": float(row.peak_close),
                    "peak_date": row.peak_date,
                    "pullback_pct": float(row.pullback_pct),
                    "ma10": float(row.ma10),
                    "ma20": float(row.ma20),
                    "ma_long": float(row.ma_long),
                    "ma_proximity": _ma_proximity(row),
                    "shrink_ratio": float(row.shrink_ratio),
                    "pullback_negative_days": int(row.pullback_negative_days),
                    "made_new_low_recent": bool(row.made_new_low_recent),
                    "reclaim_ma10": bool(row.reclaim_ma10),
                    "activity_expand": bool(row.activity_expand),
                    "stop_signal": bool(row.state == PULLBACK_STATE_RETRIGGER),
                    "flag_strategies": "strong_trend",
                },
                rank=rank,
            )
        )
    return candidates


@register
class SecondLaunchScanner(Scanner):
    strategy_id = "second_launch"
    version = SCREENING_DEFINITION_VERSION
    kind = "stock"
    description = "V2 second-launch candidates sourced exclusively from pullback-support state."
    default_params = {"top_n": 20}

    def run(self, conn: sqlite3.Connection, trade_date: str) -> list[Candidate]:
        context = build_selection_context(conn, trade_date, mode="close_final")
        evaluation = build_pullback_support(conn, trade_date, context=context)
        self.last_universe_size = len(evaluation.rows)
        persist_pullback_support_states(conn, context, evaluation)
        return select_candidates_from_pullback_support(
            evaluation,
            trade_date=trade_date,
            strategy_id=self.strategy_id,
            version=self.version,
            top_n=int(self.params["top_n"]),
        )
