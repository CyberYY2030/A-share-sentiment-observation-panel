import tempfile
import unittest
from pathlib import Path


class RuntimePathsTests(unittest.TestCase):
    def test_prefers_data_directory_when_nothing_exists(self) -> None:
        from runtime_paths import build_runtime_paths

        with tempfile.TemporaryDirectory() as tmp:
            paths = build_runtime_paths(tmp)

            base = Path(tmp)
            self.assertEqual(paths.data_dir, str(base / "data"))
            self.assertEqual(paths.stock_db, str(base / "data" / "a_share_mvp.db"))
            self.assertEqual(paths.concept_db, str(base / "data" / "ths_concept.db"))
            self.assertEqual(paths.etf_db, str(base / "data" / "etf_mvp.db"))
            self.assertEqual(paths.metrics_csv, str(base / "data" / "daily_metrics_last40.csv"))

    def test_reuses_existing_legacy_database_files(self) -> None:
        from runtime_paths import build_runtime_paths

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            legacy_stock = base / "a_share_mvp.db"
            legacy_concept = base / "ths_concept.db"
            legacy_etf = base / "etf_mvp.db"
            legacy_stock.touch()
            legacy_concept.touch()
            legacy_etf.touch()

            paths = build_runtime_paths(tmp)

            self.assertEqual(paths.stock_db, str(legacy_stock))
            self.assertEqual(paths.concept_db, str(legacy_concept))
            self.assertEqual(paths.etf_db, str(legacy_etf))

    def test_ensure_runtime_dirs_creates_data_directory(self) -> None:
        from runtime_paths import build_runtime_paths, ensure_runtime_dirs

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            paths = build_runtime_paths(tmp)

            self.assertFalse((base / "data").exists())
            ensure_runtime_dirs(paths)
            self.assertTrue((base / "data").is_dir())


if __name__ == "__main__":
    unittest.main()
