from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pandas as pd
import streamlit as st


def _db_path(base_dir: str | Path) -> Path:
    return Path(base_dir) / "mining_mvp.db"


@st.cache_data(show_spinner=False)
def _available_dates(db_path: str) -> list[str]:
    if not Path(db_path).exists():
        return []
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT DISTINCT trade_date
            FROM candidates
            WHERE strategy_id IN ('rps_stock_top20', 'rps_concept_top20')
            ORDER BY trade_date DESC
            """
        ).fetchall()
        return [row[0] for row in rows]
    finally:
        conn.close()


@st.cache_data(show_spinner=False)
def _load_rps_table(db_path: str, trade_date: str, strategy_id: str) -> pd.DataFrame:
    conn = sqlite3.connect(db_path)
    try:
        df = pd.read_sql_query(
            """
            SELECT *
            FROM candidates
            WHERE trade_date=? AND strategy_id=?
            ORDER BY rank, sec_code
            """,
            conn,
            params=[trade_date, strategy_id],
        )
        if df.empty:
            return df
        features = pd.json_normalize(df["features_json"].map(lambda text: json.loads(text)))
        return pd.concat([df.drop(columns=["features_json"]), features], axis=1)
    finally:
        conn.close()


@st.cache_data(show_spinner=False)
def _load_stock_outcome_summary(db_path: str, trade_date: str) -> dict[str, float | None]:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            """
            SELECT AVG(o.r1), AVG(o.r2), AVG(o.r3), AVG(o.r4), AVG(o.r5)
            FROM candidates c
            JOIN outcomes o ON o.candidate_id = c.candidate_id
            WHERE c.trade_date=? AND c.strategy_id='rps_stock_top20'
            """,
            (trade_date,),
        ).fetchone()
        if row is None:
            return {}
        return {"R1": row[0], "R2": row[1], "R3": row[2], "R4": row[3], "R5": row[4]}
    finally:
        conn.close()


def render_rps_tab(base_dir: str | Path) -> None:
    db_path = _db_path(base_dir)
    dates = _available_dates(str(db_path))
    if not dates:
        st.info("还没有强势榜数据，先跑一次日任务。")
        return

    trade_date = st.selectbox("查看日期", dates, key="mining_rps_date")
    stock_df = _load_rps_table(str(db_path), trade_date, "rps_stock_top20")
    concept_df = _load_rps_table(str(db_path), trade_date, "rps_concept_top20")

    top_left, top_right = st.columns(2)
    with top_left:
        st.markdown("**个股 Top20**")
        if stock_df.empty:
            st.info("这一天没有个股强势榜。")
        else:
            st.dataframe(
                stock_df[
                    ["rank", "sec_code", "sec_name", "ret_10", "ret_30", "rps_10", "rps_30", "score"]
                ],
                width="stretch",
                hide_index=True,
            )
            summary = _load_stock_outcome_summary(str(db_path), trade_date)
            if summary:
                st.caption(
                    "平均复盘: "
                    + " ".join(
                        f"{name}={value * 100:.1f}%"
                        for name, value in summary.items()
                        if value is not None
                    )
                )

    with top_right:
        st.markdown("**板块 Top20**")
        if concept_df.empty:
            st.info("这一天没有板块强势榜。")
        else:
            st.dataframe(
                concept_df[
                    ["rank", "sec_code", "sec_name", "ret_10", "ret_30", "rps_10", "rps_30", "score"]
                ],
                width="stretch",
                hide_index=True,
            )
