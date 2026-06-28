import sqlite3
import tempfile
import unittest
from pathlib import Path


class MiningDbTests(unittest.TestCase):
    def test_connect_creates_schema_and_attaches_sources(self) -> None:
        from mining.db import connect

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            sqlite3.connect(base / "a_share_mvp.db").close()
            sqlite3.connect(base / "ths_concept.db").close()

            conn = connect(base_dir=base)
            try:
                tables = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                self.assertIn("candidates", tables)
                self.assertIn("stock_market_cap", tables)

                attached = {
                    row[1] for row in conn.execute("PRAGMA database_list")
                }
                self.assertIn("ash", attached)
                self.assertIn("ths", attached)
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()

