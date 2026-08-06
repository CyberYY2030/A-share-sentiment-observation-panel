from __future__ import annotations

import sqlite3
import unittest

import pandas as pd

from mining.selection_context import SelectionContext
from mining.watchlist import (
    PULLBACK_STATE_BROKEN,
    PULLBACK_STATE_EXTEND,
    PULLBACK_STATE_PULLBACK,
    PULLBACK_STATE_READY,
    PULLBACK_STATE_RETRIGGER,
    PullbackSupportEvaluation,
    classify_pullback_support_state,
    evaluate_pullback_support,
    load_prior_pullback_states,
    persist_pullback_support_states,
)


def _context() -> SelectionContext:
    dates = pd.bdate_range("2025-01-02", periods=150).strftime("%Y-%m-%d").tolist()
    rows: list[dict[str, object]] = []
    for code, name in (("600001", "Qualified"), ("600002", "OldFlagOnly")):
        for index, trade_date in enumerate(dates):
            close = 10.0 + index * 0.1
            rows.append(
                {
                    "sec_code": code,
                    "trade_date": trade_date,
                    "adj_open": close * 0.995,
                    "adj_low": close * 0.99,
                    "adj_close": close,
                    "volume": 100.0 + index,
                }
            )
    return SelectionContext(
        bars=pd.DataFrame(rows),
        universe=pd.DataFrame(
            [
                {"sec_code": "600001", "sec_name": "Qualified"},
                {"sec_code": "600002", "sec_name": "OldFlagOnly"},
            ]
        ),
        trade_date=dates[-1],
        mode="close_final",
        as_of=f"{dates[-1]}T15:05:00+08:00",
        price_as_of=dates[-1],
        metadata_as_of=dates[-1],
        trend_profile="P120",
        data_status="ready",
        diagnostics={"clean_dates": dates},
    )


class PullbackStateMachineTests(unittest.TestCase):
    def test_five_state_boundaries_and_retrigger_requires_prior_final_state(self) -> None:
        base = {
            "pullback_pct": -0.12,
            "adj_close": 100.0,
            "ma10": 99.0,
            "ma20": 98.0,
            "ma_long": 96.0,
            "shrink_ratio": 0.5,
            "made_new_low_recent": False,
            "reclaim_ma10": False,
            "activity_expand": False,
        }
        self.assertEqual(classify_pullback_support_state({**base, "pullback_pct": -0.03}), PULLBACK_STATE_EXTEND)
        self.assertEqual(classify_pullback_support_state({**base, "ma10": 94.0}), PULLBACK_STATE_PULLBACK)
        self.assertEqual(classify_pullback_support_state(base), PULLBACK_STATE_READY)
        self.assertEqual(classify_pullback_support_state({**base, "pullback_pct": -0.31}), PULLBACK_STATE_BROKEN)
        trigger = {**base, "reclaim_ma10": True, "activity_expand": True}
        self.assertEqual(classify_pullback_support_state(trigger), PULLBACK_STATE_READY)
        self.assertEqual(
            classify_pullback_support_state(trigger, prior_states=(PULLBACK_STATE_PULLBACK,)),
            PULLBACK_STATE_RETRIGGER,
        )

    def test_eligibility_requires_shared_five_day_structure_and_a_pool_not_legacy_flags(self) -> None:
        context = _context()
        result = evaluate_pullback_support(
            context,
            a_qualified_dates={"600001": [context.trade_date]},
        )
        self.assertEqual(result.rows["sec_code"].tolist(), ["600001"])
        self.assertTrue(result.rows.iloc[0]["a_qualified_once"])
        self.assertIn(result.rows.iloc[0]["first_structure_date"], context.diagnostics["clean_dates"][-60:])
        self.assertEqual(result.diagnostics["skipped_reason_counts"], {"never_in_a_qualified_pool": 1})

    def test_intraday_does_not_write_state_history_and_close_final_does(self) -> None:
        context = _context()
        rows = pd.DataFrame([{"sec_code": "600001", "state": PULLBACK_STATE_READY}])
        evaluation = PullbackSupportEvaluation(rows=rows, trend_profile=None, diagnostics={})
        conn = sqlite3.connect(":memory:")
        try:
            intraday = SelectionContext(**{**context.__dict__, "mode": "intraday_snapshot"})
            self.assertEqual(persist_pullback_support_states(conn, intraday, evaluation), 0)
            self.assertEqual(load_prior_pullback_states(conn, "2025-12-31"), {})
            self.assertEqual(persist_pullback_support_states(conn, context, evaluation), 1)
            self.assertEqual(load_prior_pullback_states(conn, "2025-12-31"), {"600001": (PULLBACK_STATE_READY,)})
        finally:
            conn.close()
