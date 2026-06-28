# Mining Opportunity Scanner Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> Status note, 2026-04-29: this is a historical implementation plan. The mining module now exists under `mining/`, the current Streamlit entrypoint is `app.py` -> `app_panel.py`, and the old `app_panel_auto_update_v44.py` filename has been retired. Use this file for design intent, not as the current source of truth.

**Goal:** Build the offline mining scanner module, persist daily candidates and review outcomes, and surface the results in the existing Streamlit app.

**Architecture:** Add a new `mining/` package that owns its own SQLite database, attaches the existing stock and concept databases read-only, runs registered scanners, backfills next-day outcomes, and generates reports. Integrate the module into the existing Streamlit app through three display-only tabs that read the mining database.

**Tech Stack:** Python, sqlite3, pandas, streamlit, openpyxl, unittest

---

### Task 1: Lock in feature math and schema behavior

**Files:**
- Create: `D:\BaiduNetdiskDownload\cursor workflow\adata_sentiment_dashboard\tests\test_mining_features.py`
- Create: `D:\BaiduNetdiskDownload\cursor workflow\adata_sentiment_dashboard\tests\test_mining_db.py`
- Create: `D:\BaiduNetdiskDownload\cursor workflow\adata_sentiment_dashboard\mining\__init__.py`
- Create: `D:\BaiduNetdiskDownload\cursor workflow\adata_sentiment_dashboard\mining\features.py`
- Create: `D:\BaiduNetdiskDownload\cursor workflow\adata_sentiment_dashboard\mining\db.py`

- [ ] **Step 1: Write the failing feature tests**

```python
import math
import unittest


class MiningFeatureTests(unittest.TestCase):
    def test_change_pct_uses_percentage_units(self) -> None:
        from mining.features import change_pct

        value = change_pct({"close": 10.6, "pre_close": 10.0})
        self.assertAlmostEqual(value, 6.0)

    def test_upper_shadow_pct_uses_max_of_open_and_close(self) -> None:
        from mining.features import upper_shadow_pct

        value = upper_shadow_pct({"open": 10.0, "close": 10.5, "high": 11.025})
        self.assertAlmostEqual(value, 5.0)

    def test_rps_returns_percentile_scale(self) -> None:
        from mining.features import rps

        series = rps([1.0, 2.0, 3.0])
        self.assertEqual(series.iloc[-1], 100.0)
        self.assertGreater(series.iloc[1], series.iloc[0])

    def test_board_kind_handles_main_gem_and_star(self) -> None:
        from mining.features import board_kind

        self.assertEqual(board_kind("600001"), "main")
        self.assertEqual(board_kind("300001"), "gem")
        self.assertEqual(board_kind("688001"), "star")
```

- [ ] **Step 2: Write the failing database tests**

```python
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
                tables = {row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )}
                self.assertIn("candidates", tables)
                attached = {row[1] for row in conn.execute("PRAGMA database_list")}
                self.assertIn("ash", attached)
                self.assertIn("ths", attached)
            finally:
                conn.close()
```

- [ ] **Step 3: Run the targeted tests to verify they fail**

Run: `C:\Users\TY_trader1\AppData\Local\Programs\Python\Python311\python.exe -m unittest tests.test_mining_features tests.test_mining_db -v`

Expected: `ModuleNotFoundError` for `mining`

- [ ] **Step 4: Implement the minimal package, feature helpers, and schema bootstrap**

```python
def connect(base_dir: str | Path = ".", mining_db_path: str | Path | None = None):
    ...
    conn.execute("ATTACH DATABASE ? AS ash", (str(stock_db),))
    conn.execute("ATTACH DATABASE ? AS ths", (str(concept_db),))
    init_schema(conn)
    return conn
```

- [ ] **Step 5: Re-run the targeted tests**

Run: `C:\Users\TY_trader1\AppData\Local\Programs\Python\Python311\python.exe -m unittest tests.test_mining_features tests.test_mining_db -v`

Expected: all tests pass

- [ ] **Step 6: Commit**

```bash
git add tests/test_mining_features.py tests/test_mining_db.py mining/__init__.py mining/features.py mining/db.py
git commit -m "feat: add mining schema and feature helpers"
```

### Task 2: Build universe filtering and scanners

**Files:**
- Create: `D:\BaiduNetdiskDownload\cursor workflow\adata_sentiment_dashboard\tests\test_mining_scanners.py`
- Create: `D:\BaiduNetdiskDownload\cursor workflow\adata_sentiment_dashboard\mining\universe.py`
- Create: `D:\BaiduNetdiskDownload\cursor workflow\adata_sentiment_dashboard\mining\scanners\__init__.py`
- Create: `D:\BaiduNetdiskDownload\cursor workflow\adata_sentiment_dashboard\mining\scanners\momentum_breakout.py`
- Create: `D:\BaiduNetdiskDownload\cursor workflow\adata_sentiment_dashboard\mining\scanners\rps_stock.py`
- Create: `D:\BaiduNetdiskDownload\cursor workflow\adata_sentiment_dashboard\mining\scanners\rps_concept.py`
- Create: `D:\BaiduNetdiskDownload\cursor workflow\adata_sentiment_dashboard\mining\scanners\cup_handle.py`

- [ ] **Step 1: Write the failing scanner integration tests**

```python
class MiningScannerTests(unittest.TestCase):
    def test_universe_filters_non_a_share_and_st(self) -> None:
        ...

    def test_registered_scanners_return_ranked_candidates(self) -> None:
        ...
        self.assertIn("momentum_breakout", strategy_ids)
        self.assertIn("rps_stock_top20", strategy_ids)
        self.assertIn("rps_concept_top20", strategy_ids)
```

- [ ] **Step 2: Run the scanner tests to verify they fail**

Run: `C:\Users\TY_trader1\AppData\Local\Programs\Python\Python311\python.exe -m unittest tests.test_mining_scanners -v`

Expected: failures for missing scanner modules and/or wrong candidate results

- [ ] **Step 3: Implement universe filtering and the three V1 scanners**

```python
@register
class MomentumBreakoutScanner(Scanner):
    strategy_id = "momentum_breakout"
    version = "v1.0"
    kind = "stock"
    ...
```

- [ ] **Step 4: Re-run the scanner tests**

Run: `C:\Users\TY_trader1\AppData\Local\Programs\Python\Python311\python.exe -m unittest tests.test_mining_scanners -v`

Expected: all tests pass

- [ ] **Step 5: Commit**

```bash
git add tests/test_mining_scanners.py mining/universe.py mining/scanners
git commit -m "feat: add mining scanners"
```

### Task 3: Build persistence, backfill, and report generation

**Files:**
- Create: `D:\BaiduNetdiskDownload\cursor workflow\adata_sentiment_dashboard\tests\test_mining_pipeline.py`
- Create: `D:\BaiduNetdiskDownload\cursor workflow\adata_sentiment_dashboard\mining\backtest.py`
- Create: `D:\BaiduNetdiskDownload\cursor workflow\adata_sentiment_dashboard\mining\reports.py`
- Create: `D:\BaiduNetdiskDownload\cursor workflow\adata_sentiment_dashboard\mining\refresh_basics.py`
- Create: `D:\BaiduNetdiskDownload\cursor workflow\adata_sentiment_dashboard\run_daily.py`

- [ ] **Step 1: Write the failing pipeline tests**

```python
class MiningPipelineTests(unittest.TestCase):
    def test_daily_pipeline_persists_candidates_outcomes_and_reports(self) -> None:
        ...
        self.assertTrue((base / "output" / "report_2026-04-10.md").exists())
        self.assertTrue((base / "output" / "candidates_2026-04-10.xlsx").exists())
```

- [ ] **Step 2: Run the pipeline tests to verify they fail**

Run: `C:\Users\TY_trader1\AppData\Local\Programs\Python\Python311\python.exe -m unittest tests.test_mining_pipeline -v`

Expected: failures for missing pipeline modules and files

- [ ] **Step 3: Implement candidate persistence, outcome backfill, basics refresh fallback, and report writers**

```python
def execute_daily_pipeline(...):
    conn = connect(...)
    try:
        ...
        backfill_outcomes(conn, trade_date=trade_date)
        md_path = generate_markdown_report(conn, trade_date, out_dir=out_dir)
        xlsx_path = generate_excel_report(conn, trade_date, out_dir=out_dir)
        return {"trade_date": trade_date, "markdown": md_path, "excel": xlsx_path}
    finally:
        conn.close()
```

- [ ] **Step 4: Re-run the pipeline tests**

Run: `C:\Users\TY_trader1\AppData\Local\Programs\Python\Python311\python.exe -m unittest tests.test_mining_pipeline -v`

Expected: all tests pass

- [ ] **Step 5: Commit**

```bash
git add tests/test_mining_pipeline.py mining/backtest.py mining/reports.py mining/refresh_basics.py run_daily.py
git commit -m "feat: add mining pipeline and reports"
```

### Task 4: Surface mining results in Streamlit

**Files:**
- Create: `D:\BaiduNetdiskDownload\cursor workflow\adata_sentiment_dashboard\mining\streamlit_tabs\__init__.py`
- Create: `D:\BaiduNetdiskDownload\cursor workflow\adata_sentiment_dashboard\mining\streamlit_tabs\tab_scanner.py`
- Create: `D:\BaiduNetdiskDownload\cursor workflow\adata_sentiment_dashboard\mining\streamlit_tabs\tab_rps.py`
- Create: `D:\BaiduNetdiskDownload\cursor workflow\adata_sentiment_dashboard\mining\streamlit_tabs\tab_review.py`
- Modify: `D:\BaiduNetdiskDownload\cursor workflow\adata_sentiment_dashboard\app_panel.py`
- Modify: `D:\BaiduNetdiskDownload\cursor workflow\adata_sentiment_dashboard\requirements.txt`

- [ ] **Step 1: Write the failing UI smoke test**

```python
class MiningUiSmokeTests(unittest.TestCase):
    def test_streamlit_tab_helpers_import(self) -> None:
        from mining.streamlit_tabs import render_scanner_tab, render_rps_tab, render_review_tab

        self.assertTrue(callable(render_scanner_tab))
        self.assertTrue(callable(render_rps_tab))
        self.assertTrue(callable(render_review_tab))
```

- [ ] **Step 2: Run the UI smoke test to verify it fails**

Run: `C:\Users\TY_trader1\AppData\Local\Programs\Python\Python311\python.exe -m unittest tests.test_mining_ui -v`

Expected: import failure

- [ ] **Step 3: Implement the Streamlit tab renderers and wire them into the existing app**

```python
mining_scanner_tab, mining_rps_tab, mining_review_tab = st.tabs([
    "异动捕手",
    "强势榜",
    "策略复盘",
])
```

- [ ] **Step 4: Re-run the UI smoke test**

Run: `C:\Users\TY_trader1\AppData\Local\Programs\Python\Python311\python.exe -m unittest tests.test_mining_ui -v`

Expected: test passes

- [ ] **Step 5: Commit**

```bash
git add tests/test_mining_ui.py mining/streamlit_tabs app_panel.py requirements.txt
git commit -m "feat: add mining streamlit tabs"
```

### Task 5: Full verification

**Files:**
- Modify as needed from previous tasks

- [ ] **Step 1: Run the full unit test suite**

Run: `C:\Users\TY_trader1\AppData\Local\Programs\Python\Python311\python.exe -m unittest discover -s tests -v`

Expected: all tests pass

- [ ] **Step 2: Run the daily mining pipeline on real local data**

Run: `C:\Users\TY_trader1\AppData\Local\Programs\Python\Python311\python.exe run_daily.py --date 2026-04-09`

Expected: exits 0 and writes `output/report_20260409.md` plus `output/candidates_20260409.xlsx`

- [ ] **Step 3: Run the backfill command directly**

Run: `C:\Users\TY_trader1\AppData\Local\Programs\Python\Python311\python.exe -m mining.backtest --date 2026-04-09`

Expected: exits 0 and reports processed candidate count

- [ ] **Step 4: Smoke test the Streamlit app**

Run: `C:\Users\TY_trader1\AppData\Local\Programs\Python\Python311\python.exe -m streamlit run app.py --server.headless true`

Expected: app starts successfully and mining tabs render without errors

- [ ] **Step 5: Review plan coverage and fix any gaps**

Check:
- Every spec area has a concrete implementation: schema, scanners, backfill, reports, UI, CLI
- No placeholder scanner is accidentally registered
- `refresh_basics` failures degrade gracefully
- Output files are created under `output/`

- [ ] **Step 6: Commit**

```bash
git add .
git commit -m "feat: add mining opportunity scanner"
```
