import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mining.notify import build_daily_digest, send_daily_digest


class NotifyTests(unittest.TestCase):
    def _connect(self, base: Path) -> sqlite3.Connection:
        conn = sqlite3.connect(base / "mining_mvp.db")
        conn.row_factory = sqlite3.Row
        conn.execute(
            """
            CREATE TABLE watchlist_snapshots (
                snapshot_date TEXT,
                sec_code TEXT,
                sec_name TEXT,
                state TEXT,
                triage REAL,
                pullback_pct REAL,
                shrink_ratio REAL
            )
            """
        )
        conn.commit()
        return conn

    def test_build_daily_digest_handles_empty_watchlists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "output").mkdir()
            (base / "output" / "health_latest.md").write_text(
                """
# Daily Health

| domain | max_date | lag_vs_stock_days |
| --- | --- | ---: |
| stock | 2026-07-02 | 0 |
| index | 2026-07-02 | 0 |
- outcomes: {'complete': 1}; complete_delta=1
""".strip()
                + "\n",
                encoding="utf-8",
            )
            conn = self._connect(base)
            try:
                message = build_daily_digest(conn, "2026-07-02")
            finally:
                conn.close()

        self.assertIn("*回踩到位* (0)", message)
        self.assertIn("*再启动* (0)", message)
        self.assertIn("- none", message)
        self.assertIn("alerts: 0", message)

    def test_build_daily_digest_truncates_long_messages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            conn = self._connect(base)
            try:
                long_name = "长名称" * 200
                for idx in range(80):
                    conn.execute(
                        "INSERT INTO watchlist_snapshots VALUES (?, ?, ?, ?, ?, ?, ?)",
                        ("2026-07-02", f"60{idx:04d}", long_name, "回踩到位", 100 - idx, 0.05, 0.8),
                    )
                conn.commit()
                message = build_daily_digest(conn, "2026-07-02", top_n=80)
            finally:
                conn.close()

        self.assertLessEqual(len(message), 4000)
        self.assertTrue(message.endswith("... truncated"))

    def test_send_daily_digest_skips_when_unconfigured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = self._connect(Path(tmp))
            try:
                with mock.patch.dict(os.environ, {}, clear=True):
                    result = send_daily_digest(conn, "2026-07-02", sender=lambda *_: None)
            finally:
                conn.close()

        self.assertEqual(result, {"status": "skipped", "reason": "unconfigured"})

    def test_send_daily_digest_uses_config_and_injected_sender(self) -> None:
        sent: list[tuple[str, str, str]] = []
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "data").mkdir()
            (base / "data" / "notify_config.json").write_text(
                '{"TG_BOT_TOKEN":"token-value","TG_CHAT_ID":"chat-value"}',
                encoding="utf-8",
            )
            conn = self._connect(base)
            try:
                conn.execute(
                    "INSERT INTO watchlist_snapshots VALUES (?, ?, ?, ?, ?, ?, ?)",
                    ("2026-07-02", "600001", "测试股票", "再启动", 8.5, 0.031, 0.76),
                )
                conn.commit()
                with mock.patch.dict(os.environ, {}, clear=True):
                    result = send_daily_digest(conn, "2026-07-02", sender=lambda token, chat, text: sent.append((token, chat, text)))
            finally:
                conn.close()

        self.assertEqual(result["status"], "sent")
        self.assertEqual(sent[0][0], "token-value")
        self.assertEqual(sent[0][1], "chat-value")
        self.assertIn("600001 测试股票", sent[0][2])

    def test_send_daily_digest_redacts_credentials_from_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "data").mkdir()
            (base / "data" / "notify_config.json").write_text(
                '{"TG_BOT_TOKEN":"secret-token","TG_CHAT_ID":"secret-chat"}',
                encoding="utf-8",
            )
            conn = self._connect(base)
            try:
                result = send_daily_digest(
                    conn,
                    "2026-07-02",
                    sender=lambda *_: (_ for _ in ()).throw(RuntimeError("url botsecret-token chat secret-chat")),
                    max_attempts=1,
                )
            finally:
                conn.close()

        self.assertEqual(result["status"], "failed")
        self.assertNotIn("secret-token", result["error"])
        self.assertNotIn("secret-chat", result["error"])
        self.assertIn("[redacted]", result["error"])

    def test_offline_push_status_is_written_when_unconfigured(self) -> None:
        from offline_daily_update import _send_daily_push

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            health_path = base / "health_latest.md"
            health_path.write_text("# Daily Health\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {}, clear=True):
                result = _send_daily_push(base, {"path": str(health_path), "target_date": "2026-07-02"})
            body = health_path.read_text(encoding="utf-8")

        self.assertEqual(result, {"status": "skipped", "reason": "unconfigured"})
        self.assertIn("## Push", body)
        self.assertIn("push: skipped: unconfigured", body)


if __name__ == "__main__":
    unittest.main()
