"""Offline regressions for disconnected rendering and incremental recovery."""
import datetime as dt
import sqlite3
import socket
import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

import pandas as pd
import requests
from streamlit.testing.v1 import AppTest

import panel
from tests.synthetic_db import make_concept_db, make_etf_db, make_stock_db, trade_days


class OfflineQuoteTests(unittest.TestCase):
    def test_baostock_probes_bound_reads_and_restore_socket_defaults(self):
        import baostock as bs
        original = socket.getdefaulttimeout()
        self.addCleanup(socket.setdefaulttimeout, original)
        socket.setdefaulttimeout(0.25)
        for name, args, unavailable in (
            ("try_get_last_trade_day_baostock", (False,), None),
            ("try_get_trade_dates_baostock", ("2026-10-01", "2026-10-13"), []),
            ("try_get_latest_kline_day_baostock", ("sh.000001",), None),
        ):
            with self.subTest(probe=name):
                observed = []
                def stalled_login():
                    reader, writer = socket.socketpair()
                    try:
                        observed.append(reader.gettimeout())
                        reader.recv(1)  # peer sends nothing; uses a real local socket deadline
                    finally:
                        reader.close()
                        writer.close()
                with mock.patch.object(panel, "BAOSTOCK_PROBE_SOCKET_TIMEOUT", 0.01, create=True), \
                     mock.patch.object(bs, "login", side_effect=stalled_login), \
                     mock.patch.object(bs, "logout"):
                    started = time.monotonic()
                    self.assertEqual(getattr(panel, name)(*args), unavailable)
                    self.assertEqual(observed, [0.01])
                    self.assertLess(time.monotonic() - started, 0.2)
                    self.assertEqual(socket.getdefaulttimeout(), 0.25)

    def test_failed_eastmoney_quote_is_cached_and_other_sources_still_work(self):
        panel._ak_fetch_a_spot_em_result.clear()
        self.addCleanup(panel._ak_fetch_a_spot_em_result.clear)
        ak = mock.Mock()
        ak.stock_zh_a_spot_em.side_effect = requests.ConnectionError("offline")
        # Enough rows to satisfy the existing all-A quote quality gate.
        fallback = pd.DataFrame({"代码": [f"{600000+i:06d}" for i in range(1000)],
                                 "涨跌幅": [1.0] * 1000})
        with mock.patch.object(panel, "try_import_akshare", return_value=(ak, None)), \
             mock.patch.object(panel, "ak_fetch_a_spot_sina", return_value=fallback):
            for _ in range(2):
                quotes, source = panel.ak_fetch_a_spot_any()
                self.assertEqual(len(quotes), 1000)
                self.assertIn("stock_zh_a_spot|", source)
            with self.assertRaisesRegex(RuntimeError, "offline"):
                panel.ak_fetch_a_spot_em()
        ak.stock_zh_a_spot_em.assert_called_once_with()


class IncrementalRecoveryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        (self.base / "data").mkdir()
        self.days = trade_days("2026-10-13", 70)
        self.paths = {
            "stock": self.base / "data/a_share_mvp.db",
            "concept": self.base / "data/ths_concept.db",
            "etf": self.base / "data/etf_mvp.db",
        }
        make_stock_db(self.paths["stock"], self.days)
        make_concept_db(self.paths["concept"], self.days, n_concepts=120)
        make_etf_db(self.paths["etf"], self.days)
        with sqlite3.connect(self.paths["etf"]) as con:
            con.execute("CREATE TABLE etf_scale AS SELECT trade_date, '510500' AS sec_code, total_share AS shares FROM etf_total")
        self.saved = {}
        for kind, path in self.paths.items():
            with sqlite3.connect(path) as con:
                tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
                self.saved[kind] = {
                    table: con.execute(f'SELECT * FROM "{table}"').fetchall()
                    for table in tables
                    if "trade_date" in [r[1] for r in con.execute(f'PRAGMA table_info("{table}")')]
                }
        self.commands = []
        stack = ExitStack()
        self.addCleanup(stack.close)
        last = self.days[-1]
        bundle = {"calendar_last_trade_day": last, "remote_latest_stock_close_day": last,
                  "remote_latest_concept_close_day": last, "remote_latest_etf_close_day": last,
                  "t1_close_day": last, "t2_close_day": self.days[-2]}
        patches = {
            "app_dir": {"return_value": str(self.base)},
            "_now_with_tz_offset": {"return_value": dt.datetime(2026, 10, 14, 8)},
            "_cn_trade_dates_sina": {"return_value": pd.Series(pd.to_datetime(self.days + ["2026-10-14"]))},
            "discover_data_dates_bundle": {"return_value": bundle},
            "get_source_last_trade_day": {"return_value": last},
            "get_remote_latest_close_day_stock_indexes": {"return_value": last},
            "get_remote_latest_close_day_concept": {"return_value": last},
            "ak_fetch_500etf_qvix_daily": {"return_value": pd.DataFrame()},
            "run_cmd": {"side_effect": self._backfill},
        }
        for name, kwargs in patches.items():
            stack.enter_context(mock.patch.object(panel, name, **kwargs))

    def _backfill(self, cmd, **kwargs):
        self.commands.append(cmd)
        path = Path(cmd[cmd.index("--db") + 1])
        kind = next(k for k, p in self.paths.items() if p.resolve() == path.resolve())
        n = int(cmd[cmd.index("--days") + 1])
        asof = pd.Timestamp(cmd[cmd.index("--asof") + 1]).strftime("%Y-%m-%d")
        window = set([d for d in self.days if d <= asof][-n:])
        with sqlite3.connect(path) as con:
            for table, rows in self.saved[kind].items():
                cols = [r[1] for r in con.execute(f'PRAGMA table_info("{table}")')]
                pos = cols.index("trade_date")
                restored = [r for r in rows if r[pos].replace("/", "-") in window]
                con.execute(f'DELETE FROM "{table}" WHERE replace(trade_date, "/", "-") IN ({",".join("?" * len(window))})', sorted(window))
                con.executemany(f'INSERT INTO "{table}" VALUES ({",".join("?" * len(cols))})', restored)
        return 0, "synthetic daily backfill"

    def _assert_complete(self):
        for kind, path in self.paths.items():
            with sqlite3.connect(path) as con:
                for table in self.saved[kind]:
                    dates = [r[0] for r in con.execute(f'SELECT DISTINCT replace(trade_date, "/", "-") FROM "{table}" ORDER BY 1')]
                    self.assertEqual(dates, self.days, (kind, table))
        with sqlite3.connect(self.paths["stock"]) as con:
            counts = con.execute("SELECT trade_date, COUNT(*) FROM kline_daily WHERE sec_type='stock' GROUP BY trade_date ORDER BY trade_date").fetchall()
            self.assertEqual(counts, [(d, 300) for d in self.days])
            for day in self.days:
                codes = {r[0] for r in con.execute("SELECT sec_code FROM kline_daily WHERE sec_type='index' AND trade_date=?", (day,))}
                self.assertTrue(set(panel.REQUIRED_INDEX_CODES) <= codes)

    def test_auto_catchup_restores_last_25_trade_days_in_all_databases(self):
        missing = self.days[-25:]
        for kind, path in self.paths.items():
            with sqlite3.connect(path) as con:
                for table in self.saved[kind]:
                    con.execute(f'DELETE FROM "{table}" WHERE replace(trade_date, "/", "-") IN ({",".join("?" * len(missing))})', missing)
        at = AppTest.from_string("import panel\npanel.main()", default_timeout=120).run()
        self.assertEqual(list(at.exception), [])
        self.assertEqual([e.value for e in at.error], [])
        self.assertTrue(at.session_state["auto_catchup_state"]["ok"])
        self.assertEqual(len(self.commands), 3)
        for cmd in self.commands:
            n = int(cmd[cmd.index("--days") + 1])
            self.assertGreaterEqual(n, 25)
            self.assertLessEqual(self.days[-n], missing[0])
        self._assert_complete()

    def test_latest_day_repair_uses_small_windows_and_passes_validation(self):
        path = self.paths["concept"]
        with sqlite3.connect(path) as con:
            deleted = con.execute("DELETE FROM concept_kline WHERE replace(trade_date, '/', '-')=?", (self.days[-1],)).rowcount
        self.assertEqual(deleted, 120)
        ok, logs, result = panel.repair_latest_trade_day_daily(*(str(self.paths[k]) for k in ("stock", "concept", "etf")))
        self.assertTrue(ok, (logs, result))
        self.assertTrue(result["ok"])
        self.assertEqual(len(self.commands), 2)
        for cmd in self.commands:
            self.assertEqual(cmd[cmd.index("--days") + 1], "5")
        self._assert_complete()


if __name__ == "__main__":
    unittest.main()
