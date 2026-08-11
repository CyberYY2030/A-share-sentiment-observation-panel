from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from backfill_adata_ths_concept_index_kline_60d import (
    concept_target_coverage,
    ensure_tables,
    load_filtered_concepts,
    target_completion_exit_code,
)


class ConceptBackfillContractTests(unittest.TestCase):
    def test_filtered_concept_csv_accepts_gb18030_and_rejects_unvalidated_headers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gbk_file = root / "filtered_gbk.csv"
            gbk_file.write_bytes("指数代码\n880001\n880002\n".encode("gb18030"))
            bad_file = root / "filtered_bad.csv"
            bad_file.write_text("unexpected\n880001\n", encoding="utf-8")

            codes, used = load_filtered_concepts(str(gbk_file))
            with self.assertRaisesRegex(RuntimeError, "recognized code header"):
                load_filtered_concepts(str(bad_file))

        self.assertEqual(codes, {"880001", "880002"})
        self.assertEqual(used, str(gbk_file))

    def test_target_completion_is_fail_closed_but_idempotent_when_complete(self) -> None:
        incomplete = {"2026-08-07": {"missing": 1}}
        complete = {"2026-08-07": {"missing": 0}}

        self.assertEqual(target_completion_exit_code(0, ["2026-08-07"], {}, 0), 2)
        self.assertEqual(target_completion_exit_code(2, ["2026-08-07"], incomplete, 0), 1)
        self.assertEqual(target_completion_exit_code(2, ["2026-08-07"], complete, 0), 0)

    def test_target_coverage_counts_only_current_eligible_codes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = sqlite3.connect(Path(tmp) / "concept.db")
            try:
                ensure_tables(conn)
                conn.executemany(
                    "INSERT INTO concept_kline VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [
                        ("2026/08/07", "880001", 1.0, 1.0, 0.0, 1.0, 1.0),
                        ("2026/08/07", "880999", 1.0, 1.0, 0.0, 1.0, 1.0),
                    ],
                )
                conn.commit()
                coverage = concept_target_coverage(conn, ["2026-08-07"], {"880001", "880002"})
            finally:
                conn.close()

        row = coverage["2026-08-07"]
        self.assertEqual(row["have"], 1)
        self.assertEqual(row["expect"], 2)
        self.assertEqual(row["missing"], 1)
        self.assertEqual(row["expect_source"], "provider_eligible_universe")


if __name__ == "__main__":
    unittest.main()
