from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd
import streamlit as st


def _db_path(base_dir: str | Path) -> Path:
    return Path(base_dir) / "mining_mvp.db"


@st.cache_data(show_spinner=False)
def _review_frame(db_path: str, strategy_id: str) -> pd.DataFrame:
    if not Path(db_path).exists():
        return pd.DataFrame()
    conn = sqlite3.connect(db_path)
    try:
        return pd.read_sql_query(
            """
            SELECT
              c.trade_date,
              c.sec_code,
              c.sec_name,
              o.r1,
              o.r2,
              o.r3,
              o.r4,
              o.r5,
              o.is_win,
              o.status
            FROM candidates c
            LEFT JOIN outcomes o ON o.candidate_id = c.candidate_id
            WHERE c.strategy_id=?
            ORDER BY c.trade_date DESC, c.rank, c.sec_code
            """,
            conn,
            params=[strategy_id],
        )
    finally:
        conn.close()


def render_review_tab(base_dir: str | Path) -> None:
    db_path = _db_path(base_dir)
    strategy_id = st.selectbox(
        "策略",
        ["momentum_breakout", "rps_stock_top20"],
        key="mining_review_strategy",
    )
    window = st.selectbox(
        "窗口",
        [7, 14, 30, 9999],
        format_func=lambda value: "全部" if value == 9999 else f"{value}天",
    )

    review = _review_frame(str(db_path), strategy_id)
    if review.empty:
        st.info("还没有可复盘数据。")
        return

    if window != 9999:
        dates = review["trade_date"].drop_duplicates().tolist()[:window]
        review = review[review["trade_date"].isin(dates)].copy()

    reviewed = review[review["status"].notna()].copy()
    total = len(review)
    completed = len(reviewed)
    wins = int(reviewed["is_win"].fillna(0).sum()) if not reviewed.empty else 0
    win_rate = wins / completed if completed else None
    metrics = st.columns(6)
    metrics[0].metric("总候选", total)
    metrics[1].metric("已复盘", completed)
    metrics[2].metric("命中数", wins)
    metrics[3].metric("命中率", "-" if win_rate is None else f"{win_rate * 100:.1f}%")
    metrics[4].metric(
        "平均R1",
        "-" if reviewed["r1"].dropna().empty else f"{reviewed['r1'].mean() * 100:.1f}%",
    )
    metrics[5].metric(
        "平均R2",
        "-" if reviewed["r2"].dropna().empty else f"{reviewed['r2'].mean() * 100:.1f}%",
    )

    daily = (
        reviewed.groupby("trade_date", as_index=False)
        .agg(win_rate=("is_win", "mean"))
        .sort_values("trade_date")
    )
    if not daily.empty:
        st.markdown("**命中率走势**")
        st.line_chart(daily.set_index("trade_date")["win_rate"])

    histogram_source = reviewed[["r1", "r2", "r3", "r4", "r5"]].melt(
        var_name="metric", value_name="value"
    )
    histogram_source = histogram_source.dropna()
    if not histogram_source.empty:
        histogram_source["bucket"] = pd.cut(
            histogram_source["value"] * 100,
            bins=[-20, -10, -5, 0, 5, 10, 20, 50],
            include_lowest=True,
        )
        histogram_source["bucket"] = histogram_source["bucket"].astype(str)
        hist = (
            histogram_source.groupby(["metric", "bucket"], observed=False)
            .size()
            .reset_index(name="count")
        )
        st.markdown("**R1-R5 分布**")
        chart_data = hist.pivot(index="bucket", columns="metric", values="count").fillna(0)
        chart_data.index = chart_data.index.map(str)
        st.bar_chart(chart_data)

    st.markdown("**全部候选**")
    st.dataframe(review, width="stretch", hide_index=True)
