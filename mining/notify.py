from __future__ import annotations

import json
import os
import sqlite3
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

ACTION_STATES = ("回踩到位", "再启动")
MAX_TELEGRAM_CHARS = 4000


def _base_dir_from_conn(conn: sqlite3.Connection) -> Path:
    row = conn.execute("PRAGMA database_list").fetchone()
    if row is None:
        return Path.cwd()
    try:
        path = row[2]
    except Exception:
        path = row["file"]
    return Path(path).absolute().parent if path else Path.cwd()


def _load_notify_config(base_dir: str | Path) -> dict[str, str]:
    token = os.environ.get("TG_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TG_CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID")
    if token and chat_id:
        return {"bot_token": token, "chat_id": chat_id}

    config_path = Path(base_dir) / "data" / "notify_config.json"
    if not config_path.exists():
        return {}
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    token = data.get("TG_BOT_TOKEN") or data.get("bot_token") or data.get("telegram_bot_token")
    chat_id = data.get("TG_CHAT_ID") or data.get("chat_id") or data.get("telegram_chat_id")
    if token and chat_id:
        return {"bot_token": str(token), "chat_id": str(chat_id)}
    return {}


def _fmt_pct(value: Any) -> str:
    try:
        number = float(value)
    except Exception:
        return "n/a"
    return f"{number * 100:.1f}%"


def _fmt_float(value: Any) -> str:
    try:
        number = float(value)
    except Exception:
        return "n/a"
    return f"{number:.2f}"


def _snapshot_rows(conn: sqlite3.Connection, trade_date: str, state: str, limit: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT sec_code, COALESCE(sec_name, sec_code) AS sec_name, state, triage,
               pullback_pct, shrink_ratio
        FROM watchlist_snapshots
        WHERE snapshot_date=? AND state=?
        ORDER BY triage DESC, sec_code
        LIMIT ?
        """,
        (trade_date, state, int(limit)),
    ).fetchall()


def _health_lines(base_dir: Path) -> tuple[list[str], int]:
    path = base_dir / "output" / "health_latest.md"
    if not path.exists():
        return (["health: n/a"], 0)
    lines = path.read_text(encoding="utf-8").splitlines()
    freshness = [line for line in lines if line.startswith("| stock |") or line.startswith("| index |") or line.startswith("| concept |") or line.startswith("| etf |")]
    deltas = [line for line in lines if "complete_delta=" in line]
    alerts = [line for line in lines if line.startswith("[ALERT]")]
    out = []
    out.extend(freshness[:4] or ["freshness: n/a"])
    out.extend(deltas[:2])
    out.append(f"alerts: {len(alerts)}")
    return out, len(alerts)


def _truncate_message(message: str, max_chars: int = MAX_TELEGRAM_CHARS) -> str:
    if len(message) <= max_chars:
        return message
    suffix = "\n... truncated"
    return message[: max(0, max_chars - len(suffix))].rstrip() + suffix


def _safe_error_message(exc: Exception, secrets: tuple[str, ...]) -> str:
    text = str(exc)
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[redacted]")
    return f"{type(exc).__name__}: {text[:200]}"


def build_daily_digest(conn: sqlite3.Connection, trade_date: str, *, top_n: int = 10) -> str:
    base_dir = _base_dir_from_conn(conn)
    lines = [f"*A-share Daily Digest* {trade_date}", ""]
    for state in ACTION_STATES:
        rows = _snapshot_rows(conn, trade_date, state, top_n)
        lines.append(f"*{state}* ({len(rows)})")
        if not rows:
            lines.append("- none")
        for row in rows:
            lines.append(
                "- {code} {name} | triage={triage} | pullback={pullback} | shrink={shrink}".format(
                    code=row["sec_code"],
                    name=row["sec_name"],
                    triage=_fmt_float(row["triage"]),
                    pullback=_fmt_pct(row["pullback_pct"]),
                    shrink=_fmt_float(row["shrink_ratio"]),
                )
            )
        lines.append("")

    health, _alert_count = _health_lines(base_dir)
    lines.append("*Health*")
    lines.extend(f"- {line}" for line in health)
    return _truncate_message("\n".join(lines).strip())


def _post_telegram(bot_token: str, chat_id: str, text: str, timeout: float = 10.0) -> None:
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    data = urllib.parse.urlencode(
        {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": "true",
        }
    ).encode("utf-8")
    request = urllib.request.Request(url, data=data, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if response.status >= 400:
            raise RuntimeError(f"telegram sendMessage HTTP {response.status}")


def send_daily_digest(
    conn: sqlite3.Connection,
    trade_date: str,
    *,
    sender: Callable[[str, str, str], None] | None = None,
    max_attempts: int = 3,
) -> dict[str, Any]:
    base_dir = _base_dir_from_conn(conn)
    config = _load_notify_config(base_dir)
    if not config:
        return {"status": "skipped", "reason": "unconfigured"}

    message = build_daily_digest(conn, trade_date)
    send = sender or (lambda token, chat, text: _post_telegram(token, chat, text))
    attempts = max(1, int(max_attempts))
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            send(config["bot_token"], config["chat_id"], message)
            return {"status": "sent", "attempts": attempt, "chars": len(message)}
        except Exception as exc:
            last_error = _safe_error_message(exc, (config.get("bot_token", ""), config.get("chat_id", "")))
            if attempt < attempts:
                time.sleep(min(2 ** (attempt - 1), 5))
    return {"status": "failed", "attempts": attempts, "error": last_error or "unknown"}
