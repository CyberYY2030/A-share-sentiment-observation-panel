from __future__ import annotations

import json
import sqlite3
from copy import copy
from pathlib import Path

import pandas as pd


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


def _markdown_table(df: pd.DataFrame, columns: list[str]) -> str:
    if df.empty:
        return "无候选。"
    return df[columns].to_markdown(index=False)


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


def generate_markdown_report(
    conn: sqlite3.Connection, trade_date: str, out_dir: str | Path = "output"
) -> str:
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    day_key = trade_date.replace("-", "")

    momentum = _load_candidates(conn, trade_date, "momentum_breakout")
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

    lines = [
        f"# 挖掘日报 {trade_date}",
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
        "## 昨日复盘",
    ]
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
        lines.append(summary_df.to_markdown(index=False))

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

    sheets = {
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
