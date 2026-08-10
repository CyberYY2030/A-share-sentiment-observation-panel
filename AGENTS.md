# Project Agent Guide

This repository is a local A-share market sentiment dashboard and opportunity-mining scanner. Read this file before changing code or running data jobs.

## Project Shape

- `app.py` is the Streamlit entrypoint and only calls `app_panel.main()`.
- `app_panel.py` is the current main dashboard. It mixes path discovery, data freshness checks, SQLite reads/writes, sentiment calculations, live quote adapters, and Streamlit rendering.
- `runtime_paths.py` is the source of truth for root-level legacy data files and newer `data/` locations.
- `offline_daily_update.py` detects recent missing market-data domains and runs the required repair/backfill steps.
- `backfill_orchestrator.py` starts long-running backfill work outside the Streamlit render thread and logs under `output/backfill_jobs/`.
- `run_daily.py` runs the opportunity-mining daily pipeline, persists candidates/outcomes, and writes reports.
- `mining/` is the newer modular package for scanners, universe filtering, backtests, reports, and Streamlit tabs.
- `tests/` uses local synthetic SQLite fixtures where possible. Do not make unit tests depend on live market APIs.

## Read First By Task

Dashboard or UI behavior:

- `app.py`
- `app_panel.py`
- `runtime_paths.py`
- `mining/streamlit_tabs/tab_scanner.py` when the opportunity-mining tab is involved

Mining strategy or report behavior:

- `run_daily.py`
- `mining/db.py`
- `mining/universe.py`
- `mining/scanners/`
- `mining/backtest.py`
- `mining/reports.py`
- `tests/test_mining_pipeline.py`
- `tests/test_mining_ui.py`

Data freshness, repair, or path behavior:

- `runtime_paths.py`
- `offline_daily_update.py`
- `repair_market_day_akshare.py`
- `backfill_orchestrator.py`
- `backfill_baostock_hsA_60d_v2.py`
- `backfill_adata_ths_concept_index_kline_60d.py`
- `backfill_etf_equity_60d_v2.py`
- `tests/test_backfill_rules.py`
- `tests/test_runtime_paths.py`

Current opportunity-mining product logic:

- `docs/superpowers/specs/2026-04-17-current-opportunity-mining-logic.md`

## Important Local Data Boundaries

- `*.db`, `data/`, `output/`, `csv_out/`, `debug_probe/`, `V1/`, `old version/`, `mvp_store/`, `_db_schema_export/`, and `__tmp_*/` are local/generated/archive areas unless the user explicitly asks about them.
- Do not commit SQLite databases, generated reports, logs, cache files, virtualenvs, or IDE metadata.
- Runtime data can exist both at the repository root and under `data/` for backward compatibility. Check `runtime_paths.py` before changing any DB or CSV path.
- The repo often has a dirty worktree. Run `git status --short` before edits and do not revert unrelated user changes.

## Architecture Notes

- There are two generations of code: older root-level scripts and the newer `mining/` package. Prefer extending `mining/` for scanner/report logic when possible.
- `app_panel.py` is the main maintainability bottleneck. Prefer narrow extractions around DB/path resolution, market-data adapters, sentiment metric calculation, Streamlit rendering, and backfill orchestration.
- Market-data providers (`adata`, `akshare`, `baostock`) are network/runtime dependent. Use bounded probes and local tests before relying on live fetches.
- Startup freshness is split across `app_panel.py` and `offline_daily_update.py`: the panel detects visible gaps and starts background work; the offline updater owns the repair plan and domain-specific commands.
- Close-mode date selection should follow real stock/index row coverage, not only a database max date. This keeps sentiment panels and opportunity mining on the same selectable close day.
- Intraday/latest-quote opportunity results are for display. Historical candidates and outcomes are based on persisted close-data pipeline results unless a future product decision changes that boundary.

## Commands

Install dependencies:

```powershell
pip install -r requirements.txt
```

Run the dashboard:

```powershell
streamlit run app.py
```

Dry-run recent missing-data checks:

```powershell
python offline_daily_update.py --base-dir . --asof 2026-04-22 --days 10 --dry-run
```

Run recent missing-data repair:

```powershell
python offline_daily_update.py --base-dir . --asof 2026-04-22 --days 10
```

Run the schedulable daily job entrypoint and write health/log outputs:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\daily_job.ps1
```

Latest health summary: `output/health_latest.md`; state for complete-delta alerts: `output/health_state.json`; job logs: `output/backfill_jobs/`.

Daily Telegram push is optional. Credentials are read from `TG_BOT_TOKEN`/`TG_CHAT_ID` or ignored `data/notify_config.json`; never write tokens or chat IDs into code, docs, logs, or commits.

Run opportunity-mining pipeline for a date range:

```powershell
python run_daily.py --base-dir . --range 2026-04-21 2026-04-22
```

Run tests:

```powershell
python -m unittest
```

Focused checks:

```powershell
python -m unittest tests.test_mining_pipeline tests.test_mining_ui tests.test_runtime_paths
python -m unittest tests.test_backfill_rules
python -m py_compile app.py app_panel.py runtime_paths.py backfill_orchestrator.py offline_daily_update.py repair_market_day_akshare.py run_daily.py
```

## Working Rules

- Default explanations should be in Chinese; keep code, commands, paths, and identifiers in English.
- Make the smallest sufficient change that preserves product behavior and data integrity.
- Prefer modifying existing files over adding parallel wrappers or duplicate scripts.
- Do not run broad live backfills casually. Start with `--dry-run`, bounded date ranges, and explicit timeouts.
- Do not retry the same external data source more than 3 times in one turn. Switch source or report the exact failure.
- Before claiming success, run the smallest relevant verification command and report any command that could not be run.
- Keep `AGENTS.md`, `CLAUDE.md`, and `README.md` aligned when project commands, data paths, or architecture boundaries change.

