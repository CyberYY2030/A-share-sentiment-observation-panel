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
    def test_v26_c_accepts_a_price_path_without_a_history(self) -> None:
        dates = pd.bdate_range("2025-01-02", periods=150).strftime("%Y-%m-%d").tolist()
        rows = []
        for index, trade_date in enumerate(dates):
            if index < 125:
                close = 10.0
            elif index < 144:
                close = 10.0 + (index - 125) * 0.1
            elif index == 144:
                close = 14.0
            else:
                close = [13.2, 13.1, 13.0, 12.9, 13.28][index - 145]
            rows.append({
                "sec_code": "600001", "trade_date": trade_date, "adj_open": close * 0.99,
                "adj_high": close * 1.01, "adj_low": close * 0.99, "adj_close": close,
                "volume": 240.0 if index == 149 else (20.0 if index >= 145 else 100.0),
            })
        context = SelectionContext(
            bars=pd.DataFrame(rows), universe=pd.DataFrame([{"sec_code": "600001", "sec_name": "PathOnly"}]),
            trade_date=dates[-1], mode="close_final", as_of=dates[-1], price_as_of=dates[-1],
            metadata_as_of=dates[-1], trend_profile="P120", data_status="ready", diagnostics={"clean_dates": dates},
        )

        result = evaluate_pullback_support(context, a_qualified_dates={})

        self.assertEqual(result.rows["sec_code"].tolist(), ["600001"])
        self.assertFalse(result.rows.iloc[0]["a_qualified_once"])
        self.assertEqual(result.rows.iloc[0]["state"], PULLBACK_STATE_RETRIGGER)

    def test_v26_c_states_retrigger_on_confirmed_price_path_and_veto_recent_low(self) -> None:
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
        self.assertEqual(classify_pullback_support_state(trigger), PULLBACK_STATE_RETRIGGER)
        self.assertEqual(
            classify_pullback_support_state({**trigger, "made_new_low_recent": True}),
            PULLBACK_STATE_BROKEN,
        )

    def test_v26_c_does_not_require_historical_a_and_rejects_an_unqualified_price_path(self) -> None:
        context = _context()
        result = evaluate_pullback_support(
            context,
            a_qualified_dates={"600001": [context.trade_date]},
        )
        self.assertTrue(result.rows.empty)
        self.assertEqual(result.diagnostics["skipped_reason_counts"], {"v26_price_path_gate_failed": 2})

    def test_legacy_state_writer_is_disabled_for_intraday_and_close_final(self) -> None:
        context = _context()
        rows = pd.DataFrame([{"sec_code": "600001", "state": PULLBACK_STATE_READY}])
        evaluation = PullbackSupportEvaluation(rows=rows, trend_profile=None, diagnostics={})
        conn = sqlite3.connect(":memory:")
        try:
            intraday = SelectionContext(**{**context.__dict__, "mode": "intraday_snapshot"})
            self.assertEqual(persist_pullback_support_states(conn, intraday, evaluation), 0)
            self.assertEqual(load_prior_pullback_states(conn, "2025-12-31"), {})
            self.assertEqual(persist_pullback_support_states(conn, context, evaluation), 0)
            self.assertIsNone(
                conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='pullback_state_history'"
                ).fetchone()
            )
        finally:
            conn.close()
