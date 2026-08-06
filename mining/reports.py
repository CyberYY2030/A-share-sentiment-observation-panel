from __future__ import annotations

import json
import math
import sqlite3
from copy import copy
from pathlib import Path

import pandas as pd

from .capabilities import formal_definitions


def _load_candidates(
    conn: sqlite3.Connection, trade_date: str, strategy_id: str
) -> pd.DataFrame:
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
    feature_rows = df["features_json"].map(
        lambda value: json.loads(value) if value else {}
    )
    features = pd.json_normalize(feature_rows)
    return pd.concat([df.drop(columns=["features_json"]), features], axis=1)


def load_formal_capability_candidates(conn: sqlite3.Connection, trade_date: str) -> pd.DataFrame:
    """Read only persisted close-final A–E candidates using the shared capability registry."""
    definitions = formal_definitions()
    if not definitions:
        return pd.DataFrame()
    marks = ",".join("?" for _ in definitions)
    frame = pd.read_sql_query(
        f"""
        SELECT strategy_id, version, trade_date, sec_code, sec_name, entry_price, rank, features_json
        FROM candidates
        WHERE sec_type='stock' AND trade_date=? AND strategy_id IN ({marks})
        ORDER BY strategy_id, rank, sec_code
        """,
        conn,
        params=[str(trade_date), *(definition.strategy_id for definition in definitions)],
    )
    if frame.empty:
        return frame
    features = pd.json_normalize(frame["features_json"].map(lambda value: json.loads(value or "{}")))
    result = pd.concat([frame.drop(columns="features_json"), features], axis=1)
    by_strategy = {definition.strategy_id: definition for definition in definitions}
    result["capability"] = result["strategy_id"].map(lambda value: by_strategy[str(value)].capability)
    result["capability_label"] = result["strategy_id"].map(lambda value: by_strategy[str(value)].label)
    result["subtype"] = result["strategy_id"].map(lambda value: by_strategy[str(value)].subtype)
    result["reference_price"] = pd.to_numeric(result.get("reference_price", result["entry_price"]), errors="coerce").fillna(result["entry_price"])
    return result


def formal_capability_report_sections(conn: sqlite3.Connection, trade_date: str) -> list[str]:
    """Registry-driven close-final report sections; no snapshot row is queried or exported."""
    rows = load_formal_capability_candidates(conn, trade_date)
    lines = ["## 正式筛选 A–E（收盘定版）"]
    for capability in ("A", "B", "C", "D", "E"):
        definitions = formal_definitions(capability)
        label = definitions[0].label if definitions else capability
        section = rows[rows["capability"].eq(capability)].copy() if not rows.empty else pd.DataFrame()
        lines.extend(["", f"### {capability} · {label}"])
        if section.empty:
            lines.append("收盘定版后该能力无候选。")
            continue
        columns = ["rank", "sec_code", "sec_name", "reference_price", "strategy_id"]
        for field in ("event_subtype", "strength_tier", "state", "score", "path_context"):
            if field in section.columns:
                columns.append(field)
        lines.append(_markdown_table(section, columns))
    return lines


def _format_ratio(value: float | None) -> str:
    if value is None or pd.isna(value):
        return "-"
    return f"{float(value) * 100:.1f}%"


def _format_points(value: float | None) -> str:
    if value is None or pd.isna(value):
        return "-"
    return f"{float(value):.1f}%"


def _format_billion(value: float | None) -> str:
    if value is None or pd.isna(value):
        return "-"
    return f"{float(value) / 100_000_000:.1f}亿"


def _format_number(value: float | None, digits: int = 2) -> str:
    if value is None or pd.isna(value):
        return "-"
    return f"{float(value):.{digits}f}"


def _markdown_table(df: pd.DataFrame, columns: list[str]) -> str:
    if df.empty:
        return "无候选。"
    table = df[columns].copy()
    try:
        return table.to_markdown(index=False)
    except ImportError:
        header = "| " + " | ".join(map(str, table.columns)) + " |"
        divider = "| " + " | ".join("---" for _ in table.columns) + " |"
        body = ["| " + " | ".join(str(value) for value in row) + " |" for row in table.fillna("-").itertuples(index=False, name=None)]
        return "\n".join([header, divider, *body])


def get_review_summary(
    conn: sqlite3.Connection, trade_date: str, lookback: int = 30
) -> dict[str, dict[str, float | int | None]]:
    dates = pd.read_sql_query(
        """
        SELECT DISTINCT trade_date
        FROM candidates
        WHERE trade_date <= ?
        ORDER BY trade_date DESC
        LIMIT ?
        """,
        conn,
        params=[trade_date, lookback],
    )["trade_date"].tolist()
    if not dates:
        return {}
    summary = pd.read_sql_query(
        f"""
        SELECT
          c.strategy_id,
          COUNT(*) AS total_candidates,
          COUNT(o.candidate_id) AS reviewed,
          SUM(CASE WHEN o.is_win=1 THEN 1 ELSE 0 END) AS wins,
          AVG(o.r1) AS avg_r1,
          AVG(o.r2) AS avg_r2,
          AVG(o.r3) AS avg_r3
        FROM candidates c
        LEFT JOIN outcomes o ON o.candidate_id = c.candidate_id
        WHERE c.trade_date IN ({",".join("?" for _ in dates)})
        GROUP BY c.strategy_id
        """,
        conn,
        params=dates,
    )
    result: dict[str, dict[str, float | int | None]] = {}
    for row in summary.to_dict(orient="records"):
        reviewed = int(row["reviewed"] or 0)
        wins = int(row["wins"] or 0)
        result[str(row["strategy_id"])] = {
            "total_candidates": int(row["total_candidates"] or 0),
            "reviewed": reviewed,
            "wins": wins,
            "win_rate": (wins / reviewed) if reviewed else None,
            "avg_r1": row["avg_r1"],
            "avg_r2": row["avg_r2"],
            "avg_r3": row["avg_r3"],
        }
    return result


def compute_market_regime(conn: sqlite3.Connection, trade_date: str) -> dict[str, float | int | str | None]:
    rows = pd.read_sql_query(
        """
        SELECT
          today.sec_code,
          today.close,
          CASE
            WHEN today.pre_close > 0 THEN today.pre_close
            ELSE (
              SELECT previous.close
              FROM ash.kline_daily previous
              WHERE previous.sec_type='stock'
                AND previous.sec_code=today.sec_code
                AND previous.trade_date < today.trade_date
                AND previous.close > 0
              ORDER BY previous.trade_date DESC
              LIMIT 1
            )
          END AS effective_pre_close
        FROM ash.kline_daily today
        WHERE today.sec_type='stock'
          AND today.trade_date=?
        """,
        conn,
        params=[trade_date],
    )
    if rows.empty:
        return {"light": "UNKNOWN", "advancers_ratio": None, "median_pct": None, "n": 0}

    close = pd.to_numeric(rows["close"], errors="coerce")
    pre_close = pd.to_numeric(rows["effective_pre_close"], errors="coerce")
    valid = close.notna() & pre_close.gt(0)
    pct = (close[valid] / pre_close[valid] - 1.0).replace([math.inf, -math.inf], pd.NA).dropna()
    if pct.empty:
        return {"light": "UNKNOWN", "advancers_ratio": None, "median_pct": None, "n": 0}

    advancers_ratio = float(pct.gt(0).mean())
    if advancers_ratio < 0.40:
        light = "RED"
    elif advancers_ratio > 0.60:
        light = "GREEN"
    else:
        light = "YELLOW"
    return {
        "light": light,
        "advancers_ratio": advancers_ratio,
        "median_pct": float(pct.median()),
        "n": int(len(pct)),
    }


def _forward_rows_summary(
    rows: pd.DataFrame,
    source: str,
    group_column: str,
    regimes: dict[str, dict[str, float | int | str | None]],
) -> pd.DataFrame:
    columns = [
        "source",
        "group",
        "market_regime",
        "n",
        "n_t2",
        "avg_open_t2",
        "median_open_t2",
        "win_open_t2",
        "n_t5",
        "avg_open_t5",
        "median_open_t5",
        "win_open_t5",
    ]
    if rows.empty:
        return pd.DataFrame(columns=columns)

    frame = rows.copy()
    frame["open_t1"] = pd.to_numeric(frame["open_t1"], errors="coerce")
    frame["close_t2"] = pd.to_numeric(frame["close_t2"], errors="coerce")
    frame["close_t5"] = pd.to_numeric(frame["close_t5"], errors="coerce")
    frame = frame[frame["open_t1"].gt(0)].copy()
    if frame.empty:
        return pd.DataFrame(columns=columns)
    frame["market_regime"] = frame["signal_date"].map(
        lambda value: str(regimes.get(str(value), {}).get("light") or "UNKNOWN")
    )
    frame["open_t2"] = frame["close_t2"] / frame["open_t1"] - 1.0
    frame["open_t5"] = frame["close_t5"] / frame["open_t1"] - 1.0

    result_rows = []
    for (group_value, regime), group in frame.groupby([group_column, "market_regime"], dropna=False):
        t2 = pd.to_numeric(group["open_t2"], errors="coerce").dropna()
        t5 = pd.to_numeric(group["open_t5"], errors="coerce").dropna()
        result_rows.append(
            {
                "source": source,
                "group": str(group_value),
                "market_regime": str(regime),
                "n": int(len(group)),
                "n_t2": int(len(t2)),
                "avg_open_t2": float(t2.mean()) if not t2.empty else None,
                "median_open_t2": float(t2.median()) if not t2.empty else None,
                "win_open_t2": float(t2.gt(0).mean()) if not t2.empty else None,
                "n_t5": int(len(t5)),
                "avg_open_t5": float(t5.mean()) if not t5.empty else None,
                "median_open_t5": float(t5.median()) if not t5.empty else None,
                "win_open_t5": float(t5.gt(0).mean()) if not t5.empty else None,
            }
        )
    return pd.DataFrame(result_rows, columns=columns)


def get_forward_summary(
    conn: sqlite3.Connection, trade_date: str, lookback: int = 30
) -> pd.DataFrame:
    if lookback <= 0:
        return _forward_rows_summary(pd.DataFrame(), "candidate", "strategy_id", {})

    candidate_dates = pd.read_sql_query(
        "SELECT DISTINCT trade_date FROM candidates WHERE trade_date <= ?",
        conn,
        params=[trade_date],
    )["trade_date"].astype(str).tolist()
    snapshot_dates = pd.read_sql_query(
        "SELECT DISTINCT snapshot_date FROM watchlist_snapshots WHERE snapshot_date <= ?",
        conn,
        params=[trade_date],
    )["snapshot_date"].astype(str).tolist()
    dates = sorted(set(candidate_dates + snapshot_dates), reverse=True)[:lookback]
    if not dates:
        return _forward_rows_summary(pd.DataFrame(), "candidate", "strategy_id", {})
    placeholders = ",".join("?" for _ in dates)
    regimes = {day: compute_market_regime(conn, day) for day in dates}

    candidates = pd.read_sql_query(
        f"""
        SELECT c.strategy_id, c.trade_date AS signal_date,
               o.open_t1, o.close_t2, o.close_t5
        FROM candidates c
        JOIN outcomes o ON o.candidate_id=c.candidate_id
        WHERE c.sec_type='stock'
          AND c.trade_date IN ({placeholders})
        """,
        conn,
        params=dates,
    )
    watchlist = pd.read_sql_query(
        f"""
        SELECT s.state, s.snapshot_date AS signal_date,
               o.open_t1, o.close_t2, o.close_t5
        FROM watchlist_snapshots s
        JOIN watchlist_outcomes o
          ON o.snapshot_date=s.snapshot_date
         AND o.sec_code=s.sec_code
         AND o.state=s.state
        WHERE s.snapshot_date IN ({placeholders})
        """,
        conn,
        params=dates,
    )
    summary = pd.concat(
        [
            _forward_rows_summary(candidates, "candidate", "strategy_id", regimes),
            _forward_rows_summary(watchlist, "watchlist", "state", regimes),
        ],
        ignore_index=True,
    )
    if summary.empty:
        return summary
    regime_order = {"RED": 0, "YELLOW": 1, "GREEN": 2, "UNKNOWN": 3}
    summary["_regime_order"] = summary["market_regime"].map(regime_order).fillna(4)
    return summary.sort_values(["source", "group", "_regime_order"]).drop(columns="_regime_order").reset_index(drop=True)


def generate_markdown_report(
    conn: sqlite3.Connection, trade_date: str, out_dir: str | Path = "output"
) -> str:
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    day_key = trade_date.replace("-", "")

    momentum = _load_candidates(conn, trade_date, "momentum_breakout")
    launch_burst = _load_candidates(conn, trade_date, "launch_burst")
    rps_stock = _load_candidates(conn, trade_date, "rps_stock_top20")
    rps_concept = _load_candidates(conn, trade_date, "rps_concept_top20")
    previous_trade_date = conn.execute(
        "SELECT MAX(trade_date) FROM candidates WHERE trade_date < ?",
        (trade_date,),
    ).fetchone()[0]
    review = pd.DataFrame()
    if previous_trade_date:
        review = pd.read_sql_query(
            """
            SELECT
              c.strategy_id,
              c.sec_code,
              c.sec_name,
              o.r1,
              o.r2,
              o.r3,
              o.r4,
              o.r5,
              o.is_win
            FROM candidates c
            LEFT JOIN outcomes o ON o.candidate_id = c.candidate_id
            WHERE c.trade_date=? AND c.sec_type='stock'
            ORDER BY c.strategy_id, c.rank, c.sec_code
            """,
            conn,
            params=[previous_trade_date],
        )
    summary = get_review_summary(conn, trade_date, lookback=30)
    forward_summary = get_forward_summary(conn, trade_date, lookback=30)
    market_regime = compute_market_regime(conn, trade_date)

    lines = [
        f"# 挖掘日报 {trade_date}",
        "",
        (
            f"市场状态：{market_regime['light']}（上涨家数占比 "
            f"{_format_ratio(market_regime['advancers_ratio'])}，全场中位涨幅 "
            f"{_format_ratio(market_regime['median_pct'])}，样本 {market_regime['n']}）"
        ),
        "",
        *formal_capability_report_sections(conn, trade_date),
        "",
        "## 今日异动候选",
        _markdown_table(
            momentum.assign(
                涨幅=momentum.get("change_pct", pd.Series(dtype=float)).map(_format_points),
                上影=momentum.get("upper_shadow", pd.Series(dtype=float)).map(_format_points),
                成交额=momentum.get("amount", pd.Series(dtype=float)).map(_format_billion),
                五日涨幅=momentum.get("ret_5d", pd.Series(dtype=float)).map(_format_points),
            ),
            ["sec_code", "sec_name", "涨幅", "上影", "成交额", "五日涨幅", "path"]
            if not momentum.empty
            else [],
        ),
        "",
        "## 今日主升启动",
        _markdown_table(
            launch_burst.assign(
                涨幅=launch_burst.get("pct", pd.Series(dtype=float)).map(_format_ratio),
                量比=launch_burst.get("volume_ratio", pd.Series(dtype=float)).map(_format_number),
                σ倍数=launch_burst.get("sigma_multiple", pd.Series(dtype=float)).map(_format_number),
                横盘天数=launch_burst.get("cluster_days", pd.Series(dtype=float)),
            ),
            ["rank", "sec_code", "sec_name", "涨幅", "量比", "σ倍数", "横盘天数"]
            if not launch_burst.empty
            else [],
        ),
        "",
        "## 今日个股强势榜",
        _markdown_table(
            rps_stock,
            ["rank", "sec_code", "sec_name", "ret_10", "ret_30", "rps_10", "rps_30", "score"]
            if not rps_stock.empty
            else [],
        ),
        "",
        "## 今日板块强势榜",
        _markdown_table(
            rps_concept,
            ["rank", "sec_code", "sec_name", "ret_10", "ret_30", "rps_10", "rps_30", "score"]
            if not rps_concept.empty
            else [],
        ),
        "",
        "## 前向验证（次日开盘口径 × 市场状态）",
    ]
    if forward_summary.empty:
        lines.append("暂无前向验证数据。")
    else:
        forward_display = forward_summary.assign(
            开盘入T2均值=forward_summary["avg_open_t2"].map(_format_ratio),
            开盘入T2中位=forward_summary["median_open_t2"].map(_format_ratio),
            开盘入T2胜率=forward_summary["win_open_t2"].map(_format_ratio),
            开盘入T5均值=forward_summary["avg_open_t5"].map(_format_ratio),
            开盘入T5中位=forward_summary["median_open_t5"].map(_format_ratio),
            开盘入T5胜率=forward_summary["win_open_t5"].map(_format_ratio),
        ).rename(
            columns={
                "source": "来源",
                "group": "分组",
                "market_regime": "市场状态",
                "n": "有效开盘样本",
                "n_t2": "T2样本",
                "n_t5": "T5样本",
            }
        )
        lines.append(
            _markdown_table(
                forward_display,
                [
                    "来源",
                    "分组",
                    "市场状态",
                    "有效开盘样本",
                    "T2样本",
                    "开盘入T2均值",
                    "开盘入T2中位",
                    "开盘入T2胜率",
                    "T5样本",
                    "开盘入T5均值",
                    "开盘入T5中位",
                    "开盘入T5胜率",
                ],
            )
        )
    lines.extend(
        [
            "",
        "## 昨日复盘",
        ]
    )
    if review.empty:
        lines.append("无复盘数据。")
    else:
        review = review.assign(
            R1=review["r1"].map(_format_ratio),
            R2=review["r2"].map(_format_ratio),
            R3=review["r3"].map(_format_ratio),
            R4=review["r4"].map(_format_ratio),
            R5=review["r5"].map(_format_ratio),
            WIN=review["is_win"].map(
                lambda value: "-" if pd.isna(value) else ("WIN" if value else "LOSE")
            ),
        )
        lines.append(
            _markdown_table(
                review,
                ["strategy_id", "sec_code", "sec_name", "R1", "R2", "R3", "R4", "R5", "WIN"],
            )
        )

    lines.extend(["", "## 累计统计"])
    if not summary:
        lines.append("暂无统计。")
    else:
        summary_df = pd.DataFrame(
            [
                {
                    "strategy_id": strategy_id,
                    "总候选": values["total_candidates"],
                    "已复盘": values["reviewed"],
                    "命中数": values["wins"],
                    "命中率": _format_ratio(values["win_rate"]),
                    "平均R1": _format_ratio(values["avg_r1"]),
                    "平均R2": _format_ratio(values["avg_r2"]),
                    "平均R3": _format_ratio(values["avg_r3"]),
                }
                for strategy_id, values in summary.items()
            ]
        )
        lines.append(_markdown_table(summary_df, list(summary_df.columns)))

    path = out_path / f"report_{day_key}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return str(path)


def generate_excel_report(
    conn: sqlite3.Connection, trade_date: str, out_dir: str | Path = "output"
) -> str:
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    day_key = trade_date.replace("-", "")
    file_path = out_path / f"candidates_{day_key}.xlsx"

    formal_rows = load_formal_capability_candidates(conn, trade_date)
    sheets = {
        **{
            f"{definition.capability}{definition.label}{definition.subtype or definition.strategy_id}"[:31]: (
                formal_rows[formal_rows["strategy_id"].eq(definition.strategy_id)].copy()
                if not formal_rows.empty
                else pd.DataFrame()
            )
            for definition in formal_definitions()
        },
        "异动候选": _load_candidates(conn, trade_date, "momentum_breakout"),
        "个股强势": _load_candidates(conn, trade_date, "rps_stock_top20"),
        "板块强势": _load_candidates(conn, trade_date, "rps_concept_top20"),
    }
    with pd.ExcelWriter(file_path, engine="openpyxl") as writer:
        for sheet_name, frame in sheets.items():
            output = frame.copy()
            output.to_excel(writer, sheet_name=sheet_name, index=False)
            ws = writer.book[sheet_name]
            ws.freeze_panes = "A2"
            for cell in ws[1]:
                font = copy(cell.font)
                font.bold = True
                cell.font = font
            for column_cells in ws.columns:
                max_length = max(len(str(cell.value or "")) for cell in column_cells)
                ws.column_dimensions[column_cells[0].column_letter].width = min(
                    max(10, max_length + 2), 40
                )
    return str(file_path)
